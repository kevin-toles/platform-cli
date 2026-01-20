"""
Health Aggregator Service - WBS-LOG3.2

Central health aggregation for AI Platform services.
Provides `/platform/health` endpoint that polls all 6 services
and returns aggregate status with latency measurements.

AC-LOG3.1: Central `/platform/health` endpoint aggregates all 6 service health statuses

LOGGING POLICY: Silent operation by default. Only log on STATUS CHANGES.
- No logs for successful routine health checks
- Log when a service transitions healthy→unhealthy or unhealthy→healthy
- Log platform-level status changes (healthy→degraded→unhealthy)

AUTO-RESTART: When a service goes down, attempts automatic restart using topology.yaml.
- Max 3 restart attempts per service per hour
- Logs restart attempts and outcomes

Usage:
    uvicorn src.health_aggregator:app --host 0.0.0.0 --port 8088
"""
import asyncio
import subprocess
import time
import logging
import os
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from enum import Enum
from collections import defaultdict

import httpx
import yaml
from fastapi import FastAPI, Response
from pydantic import BaseModel, Field

# Configure logger - show INFO and above for status changes
logger = logging.getLogger("health_aggregator")
logger.setLevel(logging.INFO)

# Also add a stream handler if not already present (so we see output)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(handler)


class HealthStatus(str, Enum):
    """Platform health status values."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


class ServiceHealth(BaseModel):
    """Health status for a single service."""
    status: str
    latency_ms: Optional[float] = None
    error: Optional[str] = None


class PlatformHealth(BaseModel):
    """Aggregate platform health response."""
    status: HealthStatus
    timestamp: str
    services: Dict[str, ServiceHealth]


# Default service configuration
DEFAULT_SERVICES = {
    "ai-agents": "http://localhost:8082/health",
    "inference-service": "http://localhost:8085/health",
    "llm-gateway": "http://localhost:8080/health",
    "semantic-search": "http://localhost:8081/health",
    "audit-service": "http://localhost:8084/health",
    "code-orchestrator": "http://localhost:8083/health",
}

# Map health endpoint names to topology.yaml service names
SERVICE_NAME_MAP = {
    "ai-agents": "ai-agents",
    "inference-service": "inference",
    "llm-gateway": "llm-gateway",
    "semantic-search": "semantic-search",
    "audit-service": "audit-service",
    "code-orchestrator": "code-orchestrator",
}

# Restart tracking: max 3 attempts per service per hour
RESTART_ATTEMPTS: Dict[str, list] = defaultdict(list)
MAX_RESTART_ATTEMPTS = 3
RESTART_WINDOW_SECONDS = 3600  # 1 hour


def load_topology() -> Dict[str, Any]:
    """Load topology.yaml for restart commands."""
    topology_path = os.path.join(os.path.dirname(__file__), "..", "topology.yaml")
    try:
        with open(topology_path, 'r') as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.error(f"Failed to load topology.yaml: {e}")
        return {}


def attempt_restart(service_name: str) -> bool:
    """
    Attempt to restart a service using topology.yaml configuration.
    
    Returns True if restart was attempted, False if rate-limited.
    """
    # Map health check name to topology name
    topo_name = SERVICE_NAME_MAP.get(service_name, service_name)
    
    # Check rate limit
    now = time.time()
    attempts = RESTART_ATTEMPTS[service_name]
    # Remove old attempts outside the window
    attempts[:] = [t for t in attempts if now - t < RESTART_WINDOW_SECONDS]
    
    if len(attempts) >= MAX_RESTART_ATTEMPTS:
        logger.warning(
            f"⛔ RESTART RATE LIMITED: {service_name} - "
            f"{MAX_RESTART_ATTEMPTS} attempts in last hour"
        )
        return False
    
    # Record this attempt
    attempts.append(now)
    
    # Load topology and find restart command
    topology = load_topology()
    services = topology.get("services", {})
    service_config = services.get(topo_name, {})
    
    if not service_config:
        logger.error(f"❌ No topology config for service: {topo_name}")
        return False
    
    # Get the native start command (prefer native for local dev)
    start_cmds = service_config.get("start", {})
    start_cmd = start_cmds.get("native") or start_cmds.get("docker")
    
    if not start_cmd:
        logger.error(f"❌ No start command for service: {topo_name}")
        return False
    
    service_path = service_config.get("path", "")
    base_path = "/Users/kevintoles/POC"
    work_dir = os.path.join(base_path, service_path)
    
    logger.warning(f"🔄 RESTARTING: {service_name} (attempt {len(attempts)}/{MAX_RESTART_ATTEMPTS})")
    
    try:
        # Run the start command in background
        env = os.environ.copy()
        # Add any service-specific env vars
        for key, val in service_config.get("env", {}).items():
            if not val.startswith("${"):  # Skip unresolved vars
                env[key] = val
        
        # Use nohup to keep it running after this process
        subprocess.Popen(
            f"cd {work_dir} && {start_cmd}",
            shell=True,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )
        logger.info(f"🚀 RESTART INITIATED: {service_name}")
        return True
    except Exception as e:
        logger.error(f"❌ RESTART FAILED: {service_name} - {e}")
        return False


class HealthAggregator:
    """
    Aggregates health status from all platform services.
    
    Polls each service's health endpoint concurrently and
    determines overall platform health based on individual
    service statuses.
    
    LOGGING POLICY: Only logs on STATUS CHANGES (healthy↔unhealthy).
    
    Health Status Logic:
    - HEALTHY: All services responding
    - DEGRADED: 1-2 services unhealthy (platform still functional)
    - UNHEALTHY: >50% services unhealthy (platform non-functional)
    
    Attributes:
        services: Dict mapping service name to health endpoint URL
        timeout: Timeout in seconds for each health check
    """
    
    def __init__(
        self,
        services: Optional[Dict[str, str]] = None,
        timeout: float = 5.0
    ):
        """
        Initialize HealthAggregator.
        
        Args:
            services: Dict mapping service name to health endpoint URL.
                     Defaults to standard platform services.
            timeout: Timeout in seconds for each health check.
        """
        self.services = services or DEFAULT_SERVICES.copy()
        self.timeout = timeout
        # Track previous status to detect changes (only log on transitions)
        self._previous_service_status: Dict[str, str] = {}
        self._previous_platform_status: Optional[str] = None
    
    async def check_all(self) -> Dict[str, Any]:
        """
        Check health of all services concurrently.
        
        Returns:
            Dict with aggregate status, timestamp, and per-service status.
            
        Example:
            {
                "status": "degraded",
                "timestamp": "2026-01-14T12:00:00Z",
                "services": {
                    "ai-agents": {"status": "healthy", "latency_ms": 45},
                    "code-orchestrator": {"status": "unhealthy", "error": "connection refused"}
                }
            }
        """
        # Check all services concurrently
        tasks = [
            self._check_service(name, url)
            for name, url in self.services.items()
        ]
        results = await asyncio.gather(*tasks)
        
        # Build services dict from results
        services_status = {}
        for (name, _), result in zip(self.services.items(), results):
            services_status[name] = result
        
        # Determine aggregate status
        unhealthy_count = sum(
            1 for s in services_status.values()
            if s.get("status") == "unhealthy"
        )
        total_services = len(self.services)
        
        if unhealthy_count == 0:
            aggregate_status = HealthStatus.HEALTHY
        elif unhealthy_count <= 2:
            aggregate_status = HealthStatus.DEGRADED
        else:  # >50% unhealthy
            aggregate_status = HealthStatus.UNHEALTHY
        
        # LOG ONLY ON STATUS CHANGES (silent operation otherwise)
        self._log_status_changes(services_status, aggregate_status.value)
        
        return {
            "status": aggregate_status.value,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "services": services_status
        }
    
    def _log_status_changes(
        self,
        services_status: Dict[str, Dict[str, Any]],
        platform_status: str
    ) -> None:
        """
        Log only when status transitions occur. Silent otherwise.
        Triggers auto-restart when a service goes down.
        
        This implements the "alert on change" policy - no logs for routine
        successful health checks, only logs when something goes wrong or recovers.
        """
        # Check for service-level status changes
        for service_name, status_info in services_status.items():
            current_status = status_info.get("status", "unknown")
            previous_status = self._previous_service_status.get(service_name)
            
            if previous_status is not None and current_status != previous_status:
                if current_status == "unhealthy":
                    error = status_info.get("error", "unknown error")
                    logger.warning(
                        f"🔴 SERVICE DOWN: {service_name} is now unhealthy ({error})"
                    )
                    # Trigger auto-restart
                    attempt_restart(service_name)
                else:
                    logger.info(
                        f"🟢 SERVICE RECOVERED: {service_name} is now healthy"
                    )
            elif current_status == "unhealthy" and previous_status == "unhealthy":
                # Service is STILL down - log periodic reminder
                error = status_info.get("error", "unknown error")
                logger.warning(
                    f"🔴 SERVICE STILL DOWN: {service_name} ({error})"
                )
            
            # Update tracked status
            self._previous_service_status[service_name] = current_status
        
        # Check for platform-level status changes
        if self._previous_platform_status is not None and platform_status != self._previous_platform_status:
            if platform_status == "unhealthy":
                logger.error(f"🚨 PLATFORM UNHEALTHY: Multiple services down")
            elif platform_status == "degraded":
                logger.warning(f"⚠️  PLATFORM DEGRADED: Some services unavailable")
            else:
                logger.info(f"✅ PLATFORM HEALTHY: All services operational")
        
        self._previous_platform_status = platform_status

    async def _check_service(
        self,
        service_name: str,
        url: str
    ) -> Dict[str, Any]:
        """
        Check health of a single service.
        
        Args:
            service_name: Name of the service (for logging)
            url: Health endpoint URL
            
        Returns:
            Dict with status, latency_ms, and optional error message.
        """
        start_time = time.perf_counter()
        
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url)
                latency_ms = (time.perf_counter() - start_time) * 1000
                
                if response.status_code == 200:
                    return {
                        "status": "healthy",
                        "latency_ms": round(latency_ms, 2)
                    }
                else:
                    return {
                        "status": "unhealthy",
                        "latency_ms": round(latency_ms, 2),
                        "error": f"HTTP {response.status_code}"
                    }
                    
        except httpx.TimeoutException:
            latency_ms = (time.perf_counter() - start_time) * 1000
            return {
                "status": "unhealthy",
                "latency_ms": round(latency_ms, 2),
                "error": "timeout"
            }
        except httpx.ConnectError as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            return {
                "status": "unhealthy",
                "latency_ms": round(latency_ms, 2),
                "error": f"connection refused: {str(e)}"
            }
        except Exception as e:
            latency_ms = (time.perf_counter() - start_time) * 1000
            return {
                "status": "unhealthy",
                "latency_ms": round(latency_ms, 2),
                "error": str(e)
            }


# Global aggregator instance
_aggregator: Optional[HealthAggregator] = None


def get_aggregator() -> HealthAggregator:
    """Get or create the global HealthAggregator instance."""
    global _aggregator
    if _aggregator is None:
        _aggregator = HealthAggregator()
    return _aggregator


def create_app() -> FastAPI:
    """
    Create FastAPI application for health aggregation.
    
    Returns:
        Configured FastAPI application instance.
    """
    app = FastAPI(
        title="AI Platform Health Aggregator",
        description="Central health status aggregation for AI Platform services",
        version="1.0.0"
    )
    
    @app.get(
        "/platform/health",
        response_model=PlatformHealth,
        summary="Get Platform Health",
        description="Returns aggregate health status of all platform services"
    )
    async def platform_health(response: Response):
        """
        Check health of all platform services.
        
        Returns:
            - 200 OK: Platform healthy or degraded
            - 503 Service Unavailable: Platform unhealthy
        """
        aggregator = get_aggregator()
        result = await aggregator.check_all()
        
        if result["status"] == HealthStatus.UNHEALTHY.value:
            response.status_code = 503
        
        return result
    
    @app.get("/health", summary="Aggregator Health")
    async def health():
        """Health check for the aggregator service itself."""
        return {"status": "ok"}
    
    return app


# Create app instance for uvicorn
app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8088)
