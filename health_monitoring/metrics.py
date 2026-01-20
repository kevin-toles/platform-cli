"""
Prometheus Metrics for AI Platform Health - WBS-LOG3.4

Exports service health status and latency metrics for Prometheus scraping.
Provides `/metrics` endpoint in standard Prometheus text format.

AC-LOG3.2: Prometheus metrics exported at `/metrics` with `service_health_status` gauge

LOGGING POLICY: Silent operation by default.
- Prometheus scrapes `/metrics` → updates gauges → no logging
- Only health_aggregator logs on STATUS CHANGES
- No per-request logging for routine metric collection

Metrics:
- service_health_status{service="..."}: 1 = healthy, 0 = unhealthy
- service_health_latency_ms{service="..."}: Response time in milliseconds
- platform_health_status: 1 = healthy, 0.5 = degraded, 0 = unhealthy

Usage:
    # Standalone metrics server
    uvicorn src.metrics:metrics_app --host 0.0.0.0 --port 8089
    
    # Or integrate with health aggregator
    from src.metrics import collect_metrics
    await collect_metrics()
"""
import asyncio
import logging
from typing import Dict, Any, Optional

from fastapi import FastAPI, Response
from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST, CollectorRegistry

from health_monitoring.health_aggregator import HealthAggregator, get_aggregator

# Silence uvicorn access logs for metrics endpoint (very noisy)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


# Create a custom registry to allow reset in tests
REGISTRY = CollectorRegistry(auto_describe=True)

# Service-level health status gauge (1 = healthy, 0 = unhealthy)
SERVICE_HEALTH_STATUS = Gauge(
    name='service_health_status',
    documentation='Health status of AI Platform service (1=healthy, 0=unhealthy)',
    labelnames=['service'],
    registry=REGISTRY
)

# Service-level latency gauge in milliseconds
SERVICE_HEALTH_LATENCY = Gauge(
    name='service_health_latency_ms',
    documentation='Health check latency in milliseconds',
    labelnames=['service'],
    registry=REGISTRY
)

# Platform-level aggregate health status
# 1.0 = healthy, 0.5 = degraded, 0.0 = unhealthy
PLATFORM_HEALTH_STATUS = Gauge(
    name='platform_health_status',
    documentation='Overall platform health status (1=healthy, 0.5=degraded, 0=unhealthy)',
    registry=REGISTRY
)

# Mapping of status strings to numeric values
STATUS_VALUES = {
    "healthy": 1.0,
    "degraded": 0.5,
    "unhealthy": 0.0,
}


def update_metrics(health_result: Dict[str, Any]) -> None:
    """
    Update Prometheus metrics from health aggregator result.
    
    Args:
        health_result: Result from HealthAggregator.check_all()
            {
                "status": "healthy|degraded|unhealthy",
                "services": {
                    "service-name": {"status": "healthy", "latency_ms": 45}
                }
            }
    """
    # Update platform-level status
    platform_status = health_result.get("status", "unhealthy")
    PLATFORM_HEALTH_STATUS.set(STATUS_VALUES.get(platform_status, 0.0))
    
    # Update per-service metrics
    services = health_result.get("services", {})
    for service_name, service_status in services.items():
        # Set health status (1 = healthy, 0 = unhealthy)
        is_healthy = service_status.get("status") == "healthy"
        SERVICE_HEALTH_STATUS.labels(service=service_name).set(1.0 if is_healthy else 0.0)
        
        # Set latency if available
        latency = service_status.get("latency_ms")
        if latency is not None:
            SERVICE_HEALTH_LATENCY.labels(service=service_name).set(latency)


async def collect_metrics() -> Dict[str, Any]:
    """
    Collect metrics by polling the health aggregator.
    
    Returns:
        Health result dict from aggregator.
    """
    aggregator = get_aggregator()
    result = await aggregator.check_all()
    update_metrics(result)
    return result


def create_metrics_app() -> FastAPI:
    """
    Create FastAPI application for Prometheus metrics endpoint.
    
    Returns:
        Configured FastAPI application instance.
    """
    app = FastAPI(
        title="AI Platform Metrics",
        description="Prometheus metrics for AI Platform health monitoring",
        version="1.0.0"
    )
    
    @app.get("/metrics", summary="Prometheus Metrics")
    async def metrics():
        """
        Expose Prometheus metrics.
        
        Collects fresh metrics before returning.
        """
        await collect_metrics()
        return Response(
            content=generate_latest(REGISTRY),
            media_type=CONTENT_TYPE_LATEST
        )
    
    @app.get("/health", summary="Metrics Service Health")
    async def health():
        """Health check for the metrics service itself."""
        return {"status": "ok"}
    
    return app


# Create app instance for uvicorn
metrics_app = create_metrics_app()


class MetricsCollector:
    """
    Background metrics collector that periodically updates Prometheus metrics.
    
    Usage:
        collector = MetricsCollector(interval_seconds=30)
        await collector.start()  # Runs forever
        
        # Or in a context:
        async with collector:
            # Collector running in background
            pass
    """
    
    def __init__(
        self,
        interval_seconds: float = 30.0,
        aggregator: Optional[HealthAggregator] = None
    ):
        """
        Initialize MetricsCollector.
        
        Args:
            interval_seconds: How often to collect metrics (default: 30s)
            aggregator: HealthAggregator instance (defaults to global)
        """
        self.interval_seconds = interval_seconds
        self.aggregator = aggregator
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._logger = logging.getLogger("metrics_collector")
    
    async def start(self) -> None:
        """Start collecting metrics in the background."""
        self._running = True
        while self._running:
            try:
                await collect_metrics()
            except Exception as e:
                # Only log errors, not routine operations
                self._logger.error(f"Error collecting metrics: {e}")
            
            await asyncio.sleep(self.interval_seconds)
    
    async def stop(self) -> None:
        """Stop collecting metrics."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
    
    async def __aenter__(self):
        """Start collector as context manager."""
        self._task = asyncio.create_task(self.start())
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Stop collector when exiting context."""
        await self.stop()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(metrics_app, host="0.0.0.0", port=8089)
