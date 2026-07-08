"""Minimal in-process metrics (Requirement #4: at least one custom metric).

Tracks request count and error count per route template, exposed at GET /metrics.
Uses the route template (e.g. /accounts/{account_id}/balance) rather than the
raw path to avoid unbounded cardinality from account ids.
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
