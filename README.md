# Platform CLI

Command-line tools for managing the Kitchen Brigade AI Platform in hybrid mode.

## Overview

The platform runs in **hybrid mode**:
- **Databases** (Neo4j, Qdrant, Redis) run in Docker containers
- **Services** (ai-agents, llm-gateway, inference-service, etc.) run natively

## Components

### platform_supervisor.sh

Process supervisor with circuit breaker pattern for native services.

```bash
# Start all services
./platform_supervisor.sh start

# Stop all services gracefully
./platform_supervisor.sh stop

# Restart a specific service
./platform_supervisor.sh restart <service>

# Check status (including circuit breaker states)
./platform_supervisor.sh status
```

**Features:**
- Signal handling (SIGTERM/SIGINT/SIGQUIT)
- Docker container health checks before service startup
- Exponential backoff with MAX_RETRIES=5
- Circuit breaker (CLOSED → OPEN → HALF_OPEN)
- Exit code 137 (OOM) triggers immediate circuit breaker OPEN
- Graceful shutdown with configurable grace period

### Procfile

Defines the native Python services:

```
ai-agents: cd ../ai-agents && poetry run uvicorn ...
llm-gateway: cd ../llm-gateway && poetry run uvicorn ...
inference-service: cd ../inference-service && poetry run uvicorn ...
# ... etc
```

### topology.yaml

Defines service dependencies and configuration.

---

## Troubleshooting

### Neo4j Restart Loop (Stale PID)

**Symptom:** Neo4j container enters a restart loop after hard shutdown (docker kill, OOM, crash).

**Cause:** Neo4j creates PID and lock files during startup. If the container terminates without graceful shutdown, these files persist and prevent Neo4j from starting.

**Solution:** The `ai-platform-data/docker/docker-compose.yml` now includes an entrypoint wrapper that automatically cleans up stale PID/lock files before starting Neo4j.

If you need to manually clean up:

```bash
cd /path/to/ai-platform-data/docker

# Remove stale PID files
rm -f neo4j/data/neo4j.pid
rm -f neo4j/data/dbms/neo4j.pid

# Remove stale lock files (only if Neo4j is not running!)
rm -f neo4j/data/databases/neo4j/lock
rm -f neo4j/data/databases/system/lock

# Restart the container
docker compose restart neo4j
```

**Prevention:** Always use `docker compose down` or `docker compose stop` for graceful shutdown. Avoid `docker kill` unless necessary.

### Neo4j Health Check Fails

**Symptom:** Neo4j shows as unhealthy even though the container is running.

**Cause:** The old health check used HTTP (port 7474) which can respond before the Bolt protocol (port 7687) is ready.

**Solution:** The health check now uses `cypher-shell` to verify actual database availability:

```yaml
healthcheck:
  test: ["CMD", "cypher-shell", "-u", "neo4j", "-p", "devpassword", "RETURN 1"]
  interval: 10s
  timeout: 10s
  retries: 10
  start_period: 45s
```

### Service Won't Restart (Circuit Breaker OPEN)

**Symptom:** A service fails to restart and shows "breaker: OPEN" in status.

**Cause:** The circuit breaker opened after multiple failures (default: 3) or an OOM exit (code 137).

**Solution:**

```bash
# Check circuit breaker state
./platform_supervisor.sh status

# Wait for cooldown (30 seconds default), then service enters HALF_OPEN
# Or manually reset the breaker:
rm -rf .supervisor_state/<service>_breaker_*

# Restart
./platform_supervisor.sh restart <service>
```

### OOM Kills (Exit Code 137)

**Symptom:** Service exits with code 137, circuit breaker goes to OPEN.

**Cause:** The service was killed by the OS due to memory exhaustion (OOM killer).

**Solution:**
1. Check memory usage: `docker stats` or `ps aux --sort=-%mem`
2. Increase available memory or reduce service memory limits
3. For llm-gateway specifically, see WBS-PS5 (OOM Prevention)

---

## Directory Structure

```
platform-cli/
├── platform_supervisor.sh   # Main process supervisor
├── Procfile                 # Service definitions
├── topology.yaml            # Service dependencies
├── logs/                    # Service log files
├── .pids/                   # Process ID tracking
└── .supervisor_state/       # Circuit breaker state files
```

## Related Projects

- `ai-platform-data/docker/` - Docker Compose for databases
- `ai-agents/` - Main AI agents service
- `llm-gateway/` - LLM routing gateway
- `inference-service/` - Local model inference
