"""Structured JSON logging for the Account Service.

Phase 1: emits JSON logs with timestamp, level, service name, and message.
Phase 2 (cross-service): a `trace_id` field will be populated from the
propagated header so a single client request is traceable across both services.
"""
import json
import logging
import sys
from datetime import datetime, timezone

from .tracing import get_trace_id

SERVICE_NAME = "account-service"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": SERVICE_NAME,
            "traceId": get_trace_id(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Allow structured extras via logger.info("msg", extra={"extra_fields": {...}})
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
