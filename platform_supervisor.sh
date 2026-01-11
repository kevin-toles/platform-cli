#!/bin/bash
# Kitchen Brigade Platform Supervisor
# Hybrid Mode: DBs in Docker, Services Native
#
# References:
# [^1] grafana/loki signal_handler.go - SIGTERM/SIGINT handling
# [^2] cockroachdb circuit_breaker.go - probe-based health checks
# [^3] Production Kubernetes (Rosso) - startup/readiness probes
# [^4] bazel Retrier.java - ACCEPT/REJECT/TRIAL exponential backoff
# [^6] Release It! (Nygard) - circuit breaker (closed→open→half-open)
#
# NOTE: Uses file-based state storage for macOS bash 3.2 compatibility

set -eo pipefail

# =============================================================================
# Configuration
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROCFILE="${SCRIPT_DIR}/Procfile"
LOG_DIR="${SCRIPT_DIR}/logs"
PID_FILE="${SCRIPT_DIR}/.supervisor.pid"
STATE_DIR="${SCRIPT_DIR}/.supervisor_state"

# Health check configuration [^3]
HEALTH_CHECK_INTERVAL=5
HEALTH_CHECK_TIMEOUT=3
STARTUP_TIMEOUT=60

# Exponential backoff configuration [^4]
MAX_RETRIES=5
INITIAL_BACKOFF=1
MAX_BACKOFF=30

# Graceful shutdown configuration [^1]
SHUTDOWN_GRACE_PERIOD=15

# Circuit breaker configuration [^2] [^6]
# States: CLOSED (healthy), OPEN (failing), HALF_OPEN (trial)
BREAKER_FAILURE_THRESHOLD=3       # Failures before OPEN
BREAKER_COOLDOWN_SECONDS=30       # Cooldown before HALF_OPEN trial
BREAKER_SUCCESS_THRESHOLD=2       # Successes in HALF_OPEN to close
OOM_EXIT_CODE=137                 # SIGKILL/OOM - immediate OPEN

# Docker containers
DOCKER_CONTAINERS="ai-platform-neo4j ai-platform-qdrant ai-platform-redis"

# Service list (will be populated from Procfile)
SERVICES=""

# =============================================================================
# Logging
# =============================================================================

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

log_info() { log "INFO: $*"; }
log_warn() { log "WARN: $*"; }
log_error() { log "ERROR: $*" >&2; }

# =============================================================================
# State Management (File-based for bash 3.2 compatibility)
# =============================================================================

init_state_dir() {
    mkdir -p "$STATE_DIR"
}

# Get service port by name
get_service_port() {
    local service="$1"
    case "$service" in
        llm_gateway)       echo 8080 ;;
        semantic_search)   echo 8081 ;;
        ai_agents)         echo 8082 ;;
        code_orchestrator) echo 8083 ;;
        audit_service)     echo 8084 ;;
        inference_service) echo 8085 ;;
        *)                 echo 0 ;;
    esac
}

# Generic state getter/setter using files
get_state() {
    local service="$1"
    local key="$2"
    local default="${3:-}"
    local file="${STATE_DIR}/${service}.${key}"
    if [[ -f "$file" ]]; then
        cat "$file"
    else
        echo "$default"
    fi
}

set_state() {
    local service="$1"
    local key="$2"
    local value="$3"
    echo "$value" > "${STATE_DIR}/${service}.${key}"
}

# Convenience functions for circuit breaker state
get_breaker_state()    { get_state "$1" "breaker_state" "CLOSED"; }
get_failures()         { get_state "$1" "failures" "0"; }
get_successes()        { get_state "$1" "successes" "0"; }
get_open_time()        { get_state "$1" "open_time" "0"; }
get_exit_code()        { get_state "$1" "exit_code" "0"; }
get_pid()              { get_state "$1" "pid" ""; }
get_command()          { get_state "$1" "command" ""; }

set_breaker_state()    { set_state "$1" "breaker_state" "$2"; }
set_failures()         { set_state "$1" "failures" "$2"; }
set_successes()        { set_state "$1" "successes" "$2"; }
set_open_time()        { set_state "$1" "open_time" "$2"; }
set_exit_code()        { set_state "$1" "exit_code" "$2"; }
set_pid()              { set_state "$1" "pid" "$2"; }
set_command()          { set_state "$1" "command" "$2"; }

# =============================================================================
# Circuit Breaker State Machine [^2] [^4] [^6]
# States: CLOSED (accepting) → OPEN (rejecting) → HALF_OPEN (trial)
# =============================================================================

# Initialize circuit breaker for a service
init_breaker() {
    local service="$1"
    set_breaker_state "$service" "CLOSED"
    set_failures "$service" 0
    set_successes "$service" 0
    set_open_time "$service" 0
    set_exit_code "$service" 0
    log_info "Initialized circuit breaker for $service"
}

# Load breaker state (creates if doesn't exist)
load_breaker_state() {
    local service="$1"
    local state
    state=$(get_breaker_state "$service")
    if [[ "$state" == "CLOSED" ]] && [[ ! -f "${STATE_DIR}/${service}.breaker_state" ]]; then
        init_breaker "$service"
    else
        log_info "Loaded breaker state for $service: $state"
    fi
}

# Check if breaker allows restart (ACCEPT/REJECT/TRIAL decision) [^4]
breaker_allows_restart() {
    local service="$1"
    local state
    state=$(get_breaker_state "$service")
    local now
    now=$(date +%s)
    
    case "$state" in
        CLOSED)
            # ACCEPT - breaker closed, allow restart
            return 0
            ;;
        OPEN)
            # Check if cooldown has elapsed [^6]
            local open_time
            open_time=$(get_open_time "$service")
            local elapsed=$((now - open_time))
            
            if [[ $elapsed -ge $BREAKER_COOLDOWN_SECONDS ]]; then
                # Transition to HALF_OPEN for trial [^4]
                log_info "Circuit breaker for $service: OPEN → HALF_OPEN (cooldown elapsed: ${elapsed}s)"
                set_breaker_state "$service" "HALF_OPEN"
                set_successes "$service" 0
                return 0  # TRIAL - allow one restart attempt
            else
                local remaining=$((BREAKER_COOLDOWN_SECONDS - elapsed))
                log_warn "Circuit breaker for $service is OPEN, rejecting restart (cooldown: ${remaining}s remaining)"
                return 1  # REJECT - still in cooldown
            fi
            ;;
        HALF_OPEN)
            # TRIAL - already in trial mode, allow restart
            return 0
            ;;
    esac
}

# Record a failure for the circuit breaker [^2]
breaker_record_failure() {
    local service="$1"
    local exit_code="${2:-1}"
    local state
    state=$(get_breaker_state "$service")
    
    set_exit_code "$service" "$exit_code"
    local failures
    failures=$(get_failures "$service")
    failures=$((failures + 1))
    set_failures "$service" "$failures"
    set_successes "$service" 0
    
    # OOM (exit 137) triggers immediate OPEN [^6]
    if [[ $exit_code -eq $OOM_EXIT_CODE ]]; then
        log_error "Circuit breaker for $service: OOM detected (exit 137) → OPEN immediately"
        set_breaker_state "$service" "OPEN"
        set_open_time "$service" "$(date +%s)"
        return
    fi
    
    case "$state" in
        CLOSED)
            if [[ $failures -ge $BREAKER_FAILURE_THRESHOLD ]]; then
                log_error "Circuit breaker for $service: CLOSED → OPEN (failures: $failures >= threshold: $BREAKER_FAILURE_THRESHOLD)"
                set_breaker_state "$service" "OPEN"
                set_open_time "$service" "$(date +%s)"
            else
                log_warn "Circuit breaker for $service: failure $failures/$BREAKER_FAILURE_THRESHOLD (state: CLOSED)"
            fi
            ;;
        HALF_OPEN)
            # Trial failed, back to OPEN [^6]
            log_error "Circuit breaker for $service: HALF_OPEN → OPEN (trial failed)"
            set_breaker_state "$service" "OPEN"
            set_open_time "$service" "$(date +%s)"
            ;;
    esac
}

# Record a success for the circuit breaker [^2]
breaker_record_success() {
    local service="$1"
    local state
    state=$(get_breaker_state "$service")
    
    local successes
    successes=$(get_successes "$service")
    successes=$((successes + 1))
    set_successes "$service" "$successes"
    
    case "$state" in
        HALF_OPEN)
            if [[ $successes -ge $BREAKER_SUCCESS_THRESHOLD ]]; then
                log_info "Circuit breaker for $service: HALF_OPEN → CLOSED (successes: $successes >= threshold: $BREAKER_SUCCESS_THRESHOLD)"
                set_breaker_state "$service" "CLOSED"
                set_failures "$service" 0
            else
                log_info "Circuit breaker for $service: success $successes/$BREAKER_SUCCESS_THRESHOLD in HALF_OPEN"
            fi
            ;;
        CLOSED)
            # Reset failure count on success
            set_failures "$service" 0
            ;;
    esac
}

# Get human-readable breaker status
get_breaker_status() {
    local service="$1"
    local state
    state=$(get_breaker_state "$service")
    local failures
    failures=$(get_failures "$service")
    local exit_code
    exit_code=$(get_exit_code "$service")
    local extra=""
    
    if [[ "$state" == "OPEN" ]]; then
        local now
        now=$(date +%s)
        local open_time
        open_time=$(get_open_time "$service")
        local elapsed=$((now - open_time))
        local remaining=$((BREAKER_COOLDOWN_SECONDS - elapsed))
        if [[ $remaining -gt 0 ]]; then
            extra=" (cooldown: ${remaining}s)"
        else
            extra=" (ready for trial)"
        fi
    fi
    
    echo "${state}${extra} [failures: $failures, last_exit: $exit_code]"
}

# =============================================================================
# Signal Handling [^1]
# =============================================================================

cleanup() {
    log_info "Received shutdown signal, initiating graceful shutdown..."
    
    # Forward SIGTERM to all children [^1]
    for service in $SERVICES; do
        local pid
        pid=$(get_pid "$service")
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            log_info "Sending SIGTERM to $service (PID: $pid)"
            kill -TERM "$pid" 2>/dev/null || true
        fi
    done
    
    # Wait for grace period [^1]
    log_info "Waiting ${SHUTDOWN_GRACE_PERIOD}s for graceful shutdown..."
    sleep "$SHUTDOWN_GRACE_PERIOD"
    
    # Force kill remaining processes
    for service in $SERVICES; do
        local pid
        pid=$(get_pid "$service")
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            log_warn "Force killing $service (PID: $pid)"
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
    
    rm -f "$PID_FILE"
    log_info "Shutdown complete"
    exit 0
}

trap cleanup SIGTERM SIGINT SIGQUIT

# =============================================================================
# Health Checks [^2] [^3]
# =============================================================================

check_docker_container() {
    local container="$1"
    docker inspect --format='{{.State.Health.Status}}' "$container" 2>/dev/null | grep -q "healthy"
}

check_service_health() {
    local port="$1"
    local timeout="${2:-$HEALTH_CHECK_TIMEOUT}"
    
    curl -sf --connect-timeout "$timeout" "http://localhost:${port}/health" >/dev/null 2>&1
}

wait_for_docker_containers() {
    log_info "Waiting for Docker containers to be healthy..."
    
    local start_time
    start_time=$(date +%s)
    
    for container in $DOCKER_CONTAINERS; do
        log_info "Checking $container..."
        while ! check_docker_container "$container"; do
            local elapsed=$(($(date +%s) - start_time))
            if [[ $elapsed -gt $STARTUP_TIMEOUT ]]; then
                log_error "Timeout waiting for $container"
                return 1
            fi
            sleep 2
        done
        log_info "$container is healthy"
    done
    
    return 0
}

# =============================================================================
# Service Management with Exponential Backoff [^4]
# =============================================================================

start_service() {
    local name="$1"
    local command="$2"
    local log_file="${LOG_DIR}/${name}.log"
    
    mkdir -p "$LOG_DIR"
    
    # Store command for restart
    set_command "$name" "$command"
    
    log_info "Starting $name..."
    
    # Start service in background, redirect output to log file
    (
        cd "$(dirname "$command" | head -1)" 2>/dev/null || true
        eval "$command"
    ) >> "$log_file" 2>&1 &
    
    local pid=$!
    set_pid "$name" "$pid"
    
    log_info "$name started with PID $pid"
}

restart_service() {
    local name="$1"
    local command
    command=$(get_command "$name")
    
    # Check circuit breaker before restart
    if ! breaker_allows_restart "$name"; then
        return 1
    fi
    
    log_info "Restarting $name..."
    start_service "$name" "$command"
    
    # Wait for service to be ready
    if wait_for_service_ready "$name"; then
        breaker_record_success "$name"
        return 0
    else
        # Get exit code if available
        local pid
        pid=$(get_pid "$name")
        wait "$pid" 2>/dev/null || true
        local exit_code=$?
        breaker_record_failure "$name" "$exit_code"
        return 1
    fi
}

wait_for_service_ready() {
    local name="$1"
    local port
    port=$(get_service_port "$name")
    local retries=0
    local backoff=$INITIAL_BACKOFF
    
    log_info "Waiting for $name to be ready on port $port..."
    
    while [[ $retries -lt $MAX_RETRIES ]]; do
        if check_service_health "$port"; then
            log_info "$name is ready"
            return 0
        fi
        
        # Check if process is still running
        local pid
        pid=$(get_pid "$name")
        if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
            log_error "$name process died unexpectedly"
            return 1
        fi
        
        retries=$((retries + 1))
        log_info "$name not ready yet, retry $retries/$MAX_RETRIES (backoff: ${backoff}s)..."
        sleep "$backoff"
        
        # Exponential backoff with cap [^4]
        backoff=$((backoff * 2))
        if [[ $backoff -gt $MAX_BACKOFF ]]; then
            backoff=$MAX_BACKOFF
        fi
    done
    
    log_error "$name failed to become ready after $MAX_RETRIES retries"
    return 1
}

# Monitor and handle service crashes with circuit breaker
monitor_and_restart() {
    local name="$1"
    local pid
    pid=$(get_pid "$name")
    
    # Get exit code
    wait "$pid" 2>/dev/null || true
    local exit_code=$?
    
    log_error "$name (PID: $pid) exited with code $exit_code"
    set_exit_code "$name" "$exit_code"
    
    # Record failure in circuit breaker
    breaker_record_failure "$name" "$exit_code"
    
    # Attempt restart if breaker allows
    if breaker_allows_restart "$name"; then
        restart_service "$name"
    fi
}

# =============================================================================
# Status Command - Show circuit breaker states
# =============================================================================

show_status() {
    echo ""
    echo "=== Kitchen Brigade Platform Status ==="
    echo "Timestamp: $(date -Iseconds 2>/dev/null || date '+%Y-%m-%dT%H:%M:%S')"
    echo ""
    
    echo "Circuit Breaker States:"
    echo "-------------------------------------------------------------"
    printf "%-20s %-12s %-15s %-10s %s\n" "SERVICE" "PID" "STATE" "FAILURES" "LAST EXIT"
    echo "-------------------------------------------------------------"
    
    # Get services from state dir or known list
    local service_list="${SERVICES:-llm_gateway semantic_search ai_agents code_orchestrator audit_service inference_service}"
    
    for service in $service_list; do
        local pid
        pid=$(get_pid "$service")
        local state
        state=$(get_breaker_state "$service")
        local failures
        failures=$(get_failures "$service")
        local exit_code
        exit_code=$(get_exit_code "$service")
        local running_mark="x"
        
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            running_mark="+"
        fi
        
        printf "%-20s %-12s %-15s %-10s %s\n" "$service" "${pid:-N/A}$running_mark" "$state" "$failures" "$exit_code"
    done
    
    echo "-------------------------------------------------------------"
    echo ""
    echo "Circuit Breaker Config:"
    echo "  Failure threshold: $BREAKER_FAILURE_THRESHOLD"
    echo "  Cooldown seconds:  $BREAKER_COOLDOWN_SECONDS"
    echo "  Success threshold: $BREAKER_SUCCESS_THRESHOLD"
    echo "  OOM exit code:     $OOM_EXIT_CODE"
    echo ""
    echo "Legend: + = running, x = stopped"
    echo ""
}

# =============================================================================
# Main
# =============================================================================

usage() {
    echo "Usage: $0 {start|status|help}"
    echo ""
    echo "Commands:"
    echo "  start   Start all services with circuit breaker supervision"
    echo "  status  Show circuit breaker states for all services"
    echo "  help    Show this help message"
}

main() {
    local command="${1:-start}"
    
    case "$command" in
        start)
            start_supervisor
            ;;
        status)
            init_state_dir
            show_status
            ;;
        help|--help|-h)
            usage
            exit 0
            ;;
        *)
            log_error "Unknown command: $command"
            usage
            exit 1
            ;;
    esac
}

start_supervisor() {
    # Check if already running
    if [[ -f "$PID_FILE" ]]; then
        local existing_pid
        existing_pid=$(cat "$PID_FILE")
        if kill -0 "$existing_pid" 2>/dev/null; then
            log_error "Supervisor already running with PID $existing_pid"
            exit 1
        fi
        rm -f "$PID_FILE"
    fi
    
    # Initialize state directory
    init_state_dir
    
    # Save our PID
    echo $$ > "$PID_FILE"
    
    log_info "=== Kitchen Brigade Platform Supervisor ==="
    log_info "Mode: Hybrid (DBs in Docker, Services Native)"
    log_info "Circuit Breaker: Enabled (threshold=$BREAKER_FAILURE_THRESHOLD, cooldown=${BREAKER_COOLDOWN_SECONDS}s)"
    
    # Step 1: Wait for Docker containers
    if ! wait_for_docker_containers; then
        log_error "Docker containers not healthy, aborting"
        exit 1
    fi
    
    # Step 2: Parse Procfile and start services
    if [[ ! -f "$PROCFILE" ]]; then
        log_error "Procfile not found at $PROCFILE"
        exit 1
    fi
    
    log_info "Starting services from Procfile..."
    
    while IFS=: read -r name command || [[ -n "$name" ]]; do
        # Skip comments and empty lines
        [[ -z "$name" || "$name" =~ ^[[:space:]]*# ]] && continue
        
        # Trim whitespace
        name=$(echo "$name" | xargs)
        command=$(echo "$command" | xargs)
        
        [[ -z "$command" ]] && continue
        
        # Add to service list
        SERVICES="${SERVICES} ${name}"
        
        # Initialize or load circuit breaker state
        load_breaker_state "$name"
        
        start_service "$name" "$command"
        
        # Wait for service to be ready before starting next
        if wait_for_service_ready "$name"; then
            breaker_record_success "$name"
        else
            breaker_record_failure "$name" 1
            log_error "Failed to start $name, aborting"
            cleanup
            exit 1
        fi
        
    done < "$PROCFILE"
    
    # Trim leading space from SERVICES
    SERVICES="${SERVICES# }"
    
    log_info "=== All services started successfully ==="
    show_status
    
    # Keep running and monitor services
    log_info "Supervisor monitoring services... (Ctrl+C to shutdown)"
    
    while true; do
        for name in $SERVICES; do
            local pid
            pid=$(get_pid "$name")
            if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
                monitor_and_restart "$name"
            else
                # Periodic health check to record successes
                local port
                port=$(get_service_port "$name")
                if check_service_health "$port"; then
                    # Only record success if in HALF_OPEN state
                    local state
                    state=$(get_breaker_state "$name")
                    if [[ "$state" == "HALF_OPEN" ]]; then
                        breaker_record_success "$name"
                    fi
                fi
            fi
        done
        sleep "$HEALTH_CHECK_INTERVAL"
    done
}

main "$@"
