#!/bin/bash
#===============================================================================
# AI Platform - Hybrid Mode Startup Script
#===============================================================================
# Databases: Docker (Qdrant, Neo4j, Redis)
# Services: Native Python with uvicorn
#===============================================================================

POC_DIR="/Users/kevintoles/POC"
LOG_DIR="$POC_DIR/platform-cli/logs"
PID_DIR="$POC_DIR/platform-cli/pids"
export INFRASTRUCTURE_MODE=hybrid
export INFERENCE_MODELS_DIR="$POC_DIR/ai-models/models"
export INFERENCE_CONFIG_DIR="$POC_DIR/inference-service/config"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

mkdir -p "$LOG_DIR" "$PID_DIR"

log() { echo -e "${BLUE}[$(date '+%H:%M:%S')]${NC} $1"; }
success() { echo -e "${GREEN}✅ $1${NC}"; }
error() { echo -e "${RED}❌ $1${NC}"; }

check_docker_dbs() {
    log "Checking Docker databases..."
    for db in ai-platform-qdrant ai-platform-neo4j ai-platform-redis; do
        if docker ps --format '{{.Names}}' | grep -q "^${db}$"; then
            success "$db"
        else
            echo -e "${YELLOW}Starting Docker infrastructure...${NC}"
            cd "$POC_DIR/ai-platform-data/docker"
            docker-compose -f docker-compose.yml -f docker-compose.dev.yml up -d
            sleep 10
            break
        fi
    done
}

cleanup_ports() {
    log "Cleaning up ports..."
    for port in 8080 8081 8082 8083 8084 8085; do
        lsof -ti :$port 2>/dev/null | xargs kill -9 2>/dev/null || true
    done
    sleep 2
}

start_service() {
    local name=$1
    local port=$2
    local path=$3
    
    log "Starting $name on :$port..."
    
    if [ ! -d "$path/.venv" ]; then
        error "$name: no .venv at $path"
        return 1
    fi
    
    cd "$path"
    source .venv/bin/activate
    
    nohup python -m uvicorn src.main:app --host 0.0.0.0 --port $port > "$LOG_DIR/${name}.log" 2>&1 &
    
    local pid=$!
    echo $pid > "$PID_DIR/${name}.pid"
    
    local i=0
    while [ $i -lt 30 ]; do
        if curl -s "http://localhost:$port/health" > /dev/null 2>&1; then
            success "$name UP (PID: $pid)"
            return 0
        fi
        i=$((i + 1))
        sleep 1
    done
    
    error "$name FAILED"
    return 1
}

start_all() {
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║         AI PLATFORM - HYBRID MODE STARTUP                    ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""
    
    check_docker_dbs
    cleanup_ports
    
    start_service "llm-gateway" "8080" "$POC_DIR/llm-gateway"
    sleep 2
    start_service "semantic-search" "8081" "$POC_DIR/semantic-search-service"
    sleep 2
    start_service "inference-service" "8085" "$POC_DIR/inference-service"
    sleep 2
    start_service "code-orchestrator" "8083" "$POC_DIR/Code-Orchestrator-Service"
    sleep 2
    start_service "audit-service" "8084" "$POC_DIR/audit-service"
    sleep 2
    start_service "ai-agents" "8082" "$POC_DIR/ai-agents"
    
    echo ""
    show_status
}

stop_all() {
    log "Stopping all services..."
    cleanup_ports
    rm -f "$PID_DIR"/*.pid
    success "All services stopped"
}

show_status() {
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║                    PLATFORM STATUS                           ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""
    printf "%-20s %-8s %-10s\n" "SERVICE" "PORT" "STATUS"
    echo "────────────────────────────────────────────"
    
    for entry in "llm-gateway:8080" "semantic-search:8081" "ai-agents:8082" "code-orchestrator:8083" "audit-service:8084" "inference-service:8085"; do
        name="${entry%:*}"
        port="${entry#*:}"
        if curl -s "http://localhost:$port/health" > /dev/null 2>&1; then
            printf "%-20s %-8s ${GREEN}%-10s${NC}\n" "$name" ":$port" "UP"
        else
            printf "%-20s %-8s ${RED}%-10s${NC}\n" "$name" ":$port" "DOWN"
        fi
    done
    
    echo ""
    echo "Docker Databases:"
    echo "────────────────────────────────────────────"
    docker ps --format "{{.Names}}: {{.Status}}" 2>/dev/null | grep -E "qdrant|neo4j|redis" || echo "Not running"
    echo ""
}

case "${1:-start}" in
    start)   start_all ;;
    stop)    stop_all ;;
    restart) stop_all; sleep 3; start_all ;;
    status)  show_status ;;
    *)       echo "Usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac
