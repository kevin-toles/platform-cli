"""
Health Aggregator Service - WBS-LOG3.2

Central health aggregation for AI Platform services.
Provides `/platform/health` endpoint that polls all 6 services
and returns aggregate status with latency measurements.

AC-LOG3.1: Central `/platform/health` endpoint aggregates all 6 service health statuses

Usage:
    uvicorn src.health_aggregator:app --host 0.0.0.0 --port 8088
"""
import asyncio
import time
from datetime import datetime, timezone
from typing import Dict, Any, Optional
from enum import Enum

import httpx
from fastapi import FastAPI, Response
from pydantic import BaseModel, Field


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


class HealthAggregator:
    """
    Aggregates health status from all platform services.
    
    Polls each service's health endpoint concurrently and
    determines overall platform health based on individual
    service statuses.
    
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
        
        return {
            "status": aggregate_status.value,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "services": services_status
        }
    
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
