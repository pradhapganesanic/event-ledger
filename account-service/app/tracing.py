"""Trace context for the Account Service.

The Account Service is a leaf: it READS the trace ID propagated by the Gateway
from the incoming HTTP header, stores it in a contextvar so the structured
logger can include it, and echoes it back on the response. If no trace ID
arrives (e.g. the endpoint is called directly), it generates one.
"""
import uuid
from contextvars import ContextVar

TRACE_HEADER = "X-Trace-Id"

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")


def get_trace_id() -> str:
    return _trace_id.get()


def set_trace_id(trace_id: str) -> None:
    _trace_id.set(trace_id)


def new_trace_id() -> str:
    return uuid.uuid4().hex
