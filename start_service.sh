#!/bin/bash
# =============================================================================
# AI Platform Service Startup Script
# =============================================================================
#
# WBS-LOG3: Unified startup script for all services
# Reference: UNIFIED_KITCHEN_BRIGADE_ARCHITECTURE.md
#
# Features:
# - Verifies correct git branch (feature/integration)
# - Checks required files exist
# - Creates log directories
# - Registers with supervisor
# - Pre-flight health checks
#
# Usage:
#   ./start_service.sh <service-name>
#   ./start_service.sh all
#   ./start_service.sh --check-only
#
# Pattern References:
# [^1] grafana/loki signal_handler.go - Pre-flight checks
# [^2] cockroachdb circuit_breaker.go - Health validation
# [^3] Production Kubernetes - Readiness probes
# =============================================================================

set -eo pipefail

# =============================================================================
# Configuration
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
POC_DIR="/Users/kevintoles/POC"
LOG_BASE_DIR="$HOME/Library/Logs/ai-platform"
SUPERVISOR_CONF_DIR="${SCRIPT_DIR}/supervisor/conf.d"

# Expected git branch for production readiness
EXPECTED_BRANCH="feature/integration"

# Service list
SERVICES="ai-agents audit-service code-orchestrator inference-service llm-gateway semantic-search"

# Service lookup functions (macOS bash 3.2 compatible - no associative arrays)
get_service_dir() {
    case "$1" in
        ai-agents)         echo "ai-agents" ;;
        audit-service)     echo "audit-service" ;;
        code-orchestrator) echo "Code-Orchestrator-Service" ;;
        inference-service) echo "inference-service" ;;
        llm-gateway)       echo "llm-gateway" ;;
        semantic-search)   echo "semantic-search-service" ;;
        *)                 echo "" ;;
    esac
}

get_service_port() {
    case "$1" in
        ai-agents)         echo 8082 ;;
        audit-service)     echo 8084 ;;
        code-orchestrator) echo 8083 ;;
        inference-service) echo 8085 ;;
        llm-gateway)       echo 8080 ;;
        semantic-search)   echo 8081 ;;
        *)                 echo 0 ;;
    esac
}

get_required_files() {
    echo "src/main.py pyproject.toml"
}

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# =============================================================================
# Logging Functions
# =============================================================================

log() { echo -e "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
log_info() { log "${BLUE}INFO${NC}: $*"; }
log_success() { log "${GREEN}✓${NC} $*"; }
log_warn() { log "${YELLOW}⚠${NC} $*"; }
log_error() { log "${RED}✗${NC} $*" >&2; }

# =============================================================================
# Pre-Flight Check Functions
# =============================================================================

check_git_branch() {
    local service="$1"
    local service_dir="${POC_DIR}/$(get_service_dir "$service")"
    
    if [[ ! -d "$service_dir/.git" ]]; then
        log_warn "$service: Not a git repository, skipping branch check"
        return 0
    fi
    
    local current_branch
    current_branch=$(cd "$service_dir" && git branch --show-current 2>/dev/null || echo "unknown")
    
    if [[ "$current_branch" == "$EXPECTED_BRANCH" ]]; then
        log_success "$service: On correct branch ($current_branch)"
        return 0
    elif [[ "$current_branch" == "main" || "$current_branch" == "master" ]]; then
        log_success "$service: On main/master branch (acceptable)"
        return 0
    else
        log_error "$service: On branch '$current_branch', expected '$EXPECTED_BRANCH'"
        log_error "  Fix with: cd $service_dir && git checkout $EXPECTED_BRANCH"
        return 1
    fi
}

check_required_files() {
    local service="$1"
    local service_dir="${POC_DIR}/$(get_service_dir "$service")"
    local required_files
    required_files=$(get_required_files "$service")
    local missing=0
    
    for file in $required_files; do
        if [[ ! -f "$service_dir/$file" ]]; then
            log_error "$service: Missing required file: $file"
            missing=$((missing + 1))
        fi
    done
    
    if [[ $missing -eq 0 ]]; then
        log_success "$service: All required files present"
        return 0
    else
        log_error "$service: Missing $missing required file(s)"
        return 1
    fi
}

check_venv() {
    local service="$1"
    local service_dir="${POC_DIR}/$(get_service_dir "$service")"
    
    if [[ -d "$service_dir/.venv" ]]; then
        log_success "$service: Virtual environment found"
        return 0
    else
        log_warn "$service: No .venv found, may need to create"
        return 1
    fi
}

ensure_log_directory() {
    local service="$1"
    local log_dir="${LOG_BASE_DIR}/${service}"
    
    if [[ ! -d "$log_dir" ]]; then
        mkdir -p "$log_dir"
        log_info "$service: Created log directory at $log_dir"
    fi
    
    log_success "$service: Log directory ready"
}

check_port_available() {
    local service="$1"
    local port
    port=$(get_service_port "$service")
    
    if lsof -i ":$port" >/dev/null 2>&1; then
        local process
        process=$(lsof -i ":$port" | tail -1 | awk '{print $1}')
        log_warn "$service: Port $port is in use by $process"
        return 1
    else
        log_success "$service: Port $port is available"
        return 0
    fi
}

# =============================================================================
# Service Control Functions
# =============================================================================

run_preflight_checks() {
    local service="$1"
    local errors=0
    
    log_info "Running pre-flight checks for $service..."
    echo ""
    
    check_git_branch "$service" || errors=$((errors + 1))
    check_required_files "$service" || errors=$((errors + 1))
    check_venv "$service" || true  # Warning only
    ensure_log_directory "$service"
    check_port_available "$service" || errors=$((errors + 1))
    
    echo ""
    
    if [[ $errors -gt 0 ]]; then
        log_error "$service: $errors pre-flight check(s) failed"
        return 1
    else
        log_success "$service: All pre-flight checks passed"
        return 0
    fi
}

start_service_with_supervisor() {
    local service="$1"
    
    if ! command -v supervisorctl &>/dev/null; then
        log_error "supervisorctl not found. Install with: pip install supervisor"
        return 1
    fi
    
    # Check if supervisor is running
    if ! supervisorctl -c "${SCRIPT_DIR}/supervisor/supervisord.conf" status >/dev/null 2>&1; then
        log_info "Starting supervisord..."
        supervisord -c "${SCRIPT_DIR}/supervisor/supervisord.conf"
        sleep 2
    fi
    
    # Start the service
    log_info "Starting $service via supervisor..."
    supervisorctl -c "${SCRIPT_DIR}/supervisor/supervisord.conf" start "$service"
}

stop_service_with_supervisor() {
    local service="$1"
    
    if ! command -v supervisorctl &>/dev/null; then
        log_error "supervisorctl not found"
        return 1
    fi
    
    log_info "Stopping $service via supervisor..."
    supervisorctl -c "${SCRIPT_DIR}/supervisor/supervisord.conf" stop "$service" || true
}

restart_service_with_supervisor() {
    local service="$1"
    
    log_info "Restarting $service..."
    stop_service_with_supervisor "$service"
    sleep 2
    run_preflight_checks "$service" && start_service_with_supervisor "$service"
}

# =============================================================================
# Health Check Functions
# =============================================================================

wait_for_health() {
    local service="$1"
    local port
    port=$(get_service_port "$service")
    local timeout=60
    local interval=2
    local elapsed=0
    
    log_info "Waiting for $service to become healthy (timeout: ${timeout}s)..."
    
    while [[ $elapsed -lt $timeout ]]; do
        if curl -s --connect-timeout 2 "http://localhost:$port/health" >/dev/null 2>&1; then
            log_success "$service is healthy (took ${elapsed}s)"
            return 0
        fi
        sleep $interval
        elapsed=$((elapsed + interval))
    done
    
    log_error "$service did not become healthy within ${timeout}s"
    return 1
}

# =============================================================================
# Main Entry Points
# =============================================================================

check_all_services() {
    log_info "Running pre-flight checks for all services..."
    echo ""
    
    local total_errors=0
    
    for service in $SERVICES; do
        echo "═══════════════════════════════════════════════════════════════════"
        if ! run_preflight_checks "$service"; then
            total_errors=$((total_errors + 1))
        fi
        echo ""
    done
    
    echo "═══════════════════════════════════════════════════════════════════"
    if [[ $total_errors -gt 0 ]]; then
        log_error "SUMMARY: $total_errors service(s) have failing pre-flight checks"
        return 1
    else
        log_success "SUMMARY: All services passed pre-flight checks"
        return 0
    fi
}

start_all_services() {
    log_info "Starting all services..."
    
    # Start infrastructure first (Docker containers)
    log_info "Ensuring infrastructure containers are running..."
    cd "${POC_DIR}/ai-platform-data/docker" && docker-compose up -d neo4j qdrant redis 2>/dev/null || true
    sleep 5
    
    # Start services in dependency order
    local order="llm-gateway semantic-search inference-service audit-service ai-agents code-orchestrator"
    
    for service in $order; do
        echo ""
        echo "═══════════════════════════════════════════════════════════════════"
        if run_preflight_checks "$service"; then
            start_service_with_supervisor "$service"
            wait_for_health "$service" || log_warn "Service started but health check failed"
        else
            log_error "Skipping $service due to failed pre-flight checks"
        fi
    done
}

fix_branch() {
    local service="$1"
    local service_dir="${POC_DIR}/$(get_service_dir "$service")"
    
    log_info "Switching $service to $EXPECTED_BRANCH..."
    
    cd "$service_dir"
    
    # Stash any local changes
    if git diff --quiet && git diff --cached --quiet; then
        log_info "No local changes to stash"
    else
        log_info "Stashing local changes..."
        git stash push -m "Auto-stash before branch switch"
    fi
    
    # Fetch and checkout
    git fetch origin "$EXPECTED_BRANCH" 2>/dev/null || true
    git checkout "$EXPECTED_BRANCH"
    
    log_success "$service is now on $EXPECTED_BRANCH"
}

# =============================================================================
# CLI Interface
# =============================================================================

show_usage() {
    echo "AI Platform Service Startup Script"
    echo ""
    echo "Usage: $0 <command> [service-name]"
    echo ""
    echo "Commands:"
    echo "  check <service>    Run pre-flight checks for a service"
    echo "  check-all          Run pre-flight checks for all services"
    echo "  start <service>    Start a specific service"
    echo "  start-all          Start all services"
    echo "  stop <service>     Stop a specific service"
    echo "  restart <service>  Restart a specific service"
    echo "  fix-branch <svc>   Switch service to $EXPECTED_BRANCH"
    echo "  status             Show supervisor status"
    echo ""
    echo "Available services:"
    for service in $SERVICES; do
        echo "  - $service (port $(get_service_port "$service"))"
    done
}

main() {
    local command="${1:-}"
    local service="${2:-}"
    
    case "$command" in
        check)
            if [[ -z "$service" ]]; then
                log_error "Please specify a service name"
                exit 1
            fi
            run_preflight_checks "$service"
            ;;
        check-all|--check-only)
            check_all_services
            ;;
        start)
            if [[ -z "$service" ]]; then
                log_error "Please specify a service name"
                exit 1
            fi
            run_preflight_checks "$service" && start_service_with_supervisor "$service"
            ;;
        start-all|all)
            start_all_services
            ;;
        stop)
            if [[ -z "$service" ]]; then
                log_error "Please specify a service name"
                exit 1
            fi
            stop_service_with_supervisor "$service"
            ;;
        restart)
            if [[ -z "$service" ]]; then
                log_error "Please specify a service name"
                exit 1
            fi
            restart_service_with_supervisor "$service"
            ;;
        fix-branch)
            if [[ -z "$service" ]]; then
                log_error "Please specify a service name"
                exit 1
            fi
            fix_branch "$service"
            ;;
        status)
            supervisorctl -c "${SCRIPT_DIR}/supervisor/supervisord.conf" status
            ;;
        help|--help|-h)
            show_usage
            ;;
        *)
            show_usage
            exit 1
            ;;
    esac
}

main "$@"
