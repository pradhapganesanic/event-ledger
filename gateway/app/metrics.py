"""Prometheus metrics for the Event Gateway (Req #4: custom metric).

Exposes GET /metrics in Prometheus text format via the prometheus-client
library. Provides:
  - http_requests_total{method,endpoint,status}      request count + error rate
  - http_request_duration_seconds{method,endpoint}   latency histogram
  - gateway_events_total{outcome}                    CUSTOM domain counter

The middleware labels by route TEMPLATE, not the raw path, to keep label
cardinality bounded.
"""
import time

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

REQUEST_COUNT = Counter(
    "http_requests_total", "Total HTTP requests", ["method", "endpoint", "status"]
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds", "HTTP request latency (seconds)", ["method", "endpoint"]
)
# Custom domain metric: events processed, labelled by outcome
# (stored | duplicate | rejected | failed).
EVENTS = Counter("gateway_events_total", "Events processed by outcome", ["outcome"])


def install(app: FastAPI) -> None:
    @app.middleware("http")
    async def _metrics_middleware(request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        endpoint = getattr(route, "path", request.url.path)
        REQUEST_LATENCY.labels(request.method, endpoint).observe(time.perf_counter() - start)
        REQUEST_COUNT.labels(request.method, endpoint, response.status_code).inc()
        return response

    @app.get("/metrics")
    def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
