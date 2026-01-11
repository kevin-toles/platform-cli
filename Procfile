# Kitchen Brigade Platform - Hybrid Mode Procfile
# Reference: [^1] grafana/loki signal_handler.go pattern
# Reference: [^2] Production Kubernetes - service startup ordering

# =============================================================================
# INFRASTRUCTURE LAYER (Docker containers - managed by docker-compose)
# These are NOT started by foreman but health-checked before services start
# =============================================================================
# neo4j: docker container ai-platform-neo4j (port 7687)
# qdrant: docker container ai-platform-qdrant (port 6333)
# redis: docker container ai-platform-redis (port 6379)

# =============================================================================
# APPLICATION LAYER (Native Python services)
# Started in dependency order with health checks
# =============================================================================

# Gateway layer - entry point for LLM requests
llm_gateway: cd /Users/kevintoles/POC/llm-gateway && source .venv/bin/activate && python -m uvicorn src.main:app --host 0.0.0.0 --port 8080

# Search layer - requires Qdrant healthy
semantic_search: cd /Users/kevintoles/POC/semantic-search-service && source .venv/bin/activate && python -m uvicorn src.main:app --host 0.0.0.0 --port 8081

# Agents layer - requires semantic-search healthy
ai_agents: cd /Users/kevintoles/POC/ai-agents && source .venv/bin/activate && python -m uvicorn src.main:app --host 0.0.0.0 --port 8082

# Code orchestrator - requires ai-agents healthy
code_orchestrator: cd /Users/kevintoles/POC/Code-Orchestrator-Service && source .venv/bin/activate && python -m uvicorn src.main:app --host 0.0.0.0 --port 8083

# Audit service - independent
audit_service: cd /Users/kevintoles/POC/audit-service && source .venv/bin/activate && python -m uvicorn src.main:app --host 0.0.0.0 --port 8084

# Inference service - requires GPU (Metal on macOS), independent
inference_service: cd /Users/kevintoles/POC/inference-service && source .venv/bin/activate && INFERENCE_GPU_LAYERS=-1 INFERENCE_MODELS_DIR=/Users/kevintoles/POC/ai-models/models python -m uvicorn src.main:app --host 0.0.0.0 --port 8085
