"""Minimal in-process metrics for the Gateway (Requirement #4).

Per-route request and error counts, exposed at GET /metrics.
"""
from collections import defaultdict

from fastapi import FastAPI, Request

_request_counts: dict[str, int] = defaultdict(int)
_error_counts: dict[str, int] = defaultdict(int)


def install(app: FastAPI, service_name: str) -> None:
    @app.middleware("http")
    async def _count_requests(request: Request, call_next):
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        key = f"{request.method} {path}"
        _request_counts[key] += 1
        if response.status_code >= 500:
            _error_counts[key] += 1
        return response

    @app.get("/metrics")
    def metrics():
        return {
            "service": service_name,
            "requestsByEndpoint": dict(_request_counts),
            "errorsByEndpoint": dict(_error_counts),
        }
