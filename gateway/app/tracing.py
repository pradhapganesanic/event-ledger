"""Trace context for the Event Gateway.

The Gateway is the trace ORIGIN: for each incoming request it generates a trace
ID (or honours one already supplied by an upstream caller), stores it in a
contextvar so the structured logger and the Account Service client can read it,
propagates it downstream via an HTTP header, and echoes it on the response.
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
