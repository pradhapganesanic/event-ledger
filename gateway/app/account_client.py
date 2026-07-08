"""HTTP client for the internal Account Service.

Responsibilities:
  - propagate the current trace ID downstream via the X-Trace-Id header
  - apply a request timeout so a slow/hung Account Service cannot block the
    Gateway indefinitely
  - wrap calls in a circuit breaker (Req #5) so repeated failures trip the
    breaker OPEN and the Gateway fails fast instead of hammering a down service
  - translate transport errors, 5xx responses, and an OPEN breaker into a single
    AccountServiceUnavailable signal the Gateway maps to HTTP 503

The client is a module-level singleton so tests can swap in a transport via
set_client(); reset_client() restores the default.
"""
import logging
import os

import httpx

from .logging_config import SERVICE_NAME
from .otel import instrument_httpx_client
from .resiliency import CircuitOpenError, get_breaker
from .tracing import TRACE_HEADER, get_trace_id

log = logging.getLogger(SERVICE_NAME)

ACCOUNT_SERVICE_URL = os.getenv("ACCOUNT_SERVICE_URL", "http://localhost:8001")
TIMEOUT_SECONDS = float(os.getenv("ACCOUNT_TIMEOUT_SECONDS", "3.0"))
BREAKER_FAILURE_THRESHOLD = int(os.getenv("ACCOUNT_BREAKER_THRESHOLD", "5"))
BREAKER_RECOVERY_TIMEOUT = float(os.getenv("ACCOUNT_BREAKER_RECOVERY_SECONDS", "10.0"))

_client: httpx.Client | None = None


class AccountServiceUnavailable(Exception):
    """Raised when the Account Service is unreachable, timed out, or 5xx'd."""


# One breaker guards every call to the Account Service. Only
# AccountServiceUnavailable counts as a failure — a 404 from a balance query is
# a normal response, not a fault.
account_breaker = get_breaker(
    "account-service",
    failure_threshold=BREAKER_FAILURE_THRESHOLD,
    recovery_timeout=BREAKER_RECOVERY_TIMEOUT,
    failure_exceptions=(AccountServiceUnavailable,),
)


def _guarded(fn):
    """Run fn through the breaker; convert an OPEN breaker into the 503 signal."""
    try:
        return account_breaker.call(fn)
    except CircuitOpenError as exc:
        raise AccountServiceUnavailable(str(exc)) from exc


def get_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = instrument_httpx_client(
            httpx.Client(base_url=ACCOUNT_SERVICE_URL, timeout=TIMEOUT_SECONDS)
        )
    return _client


def set_client(client: httpx.Client | None) -> None:
    """Override the client (tests). Pass None via reset_client() to restore."""
    global _client
    _client = instrument_httpx_client(client) if client is not None else None


def reset_client() -> None:
    set_client(None)


def _headers() -> dict:
    return {TRACE_HEADER: get_trace_id()}


def apply_transaction(account_id: str, payload: dict) -> tuple[int, dict]:
    """Apply a transaction on the Account Service. Returns (status_code, body).

    Raises AccountServiceUnavailable on transport error, timeout, 5xx, or an
    OPEN circuit breaker.
    """

    def _do():
        try:
            resp = get_client().post(
                f"/accounts/{account_id}/transactions", json=payload, headers=_headers()
            )
        except httpx.RequestError as exc:
            raise AccountServiceUnavailable(f"request error: {exc}") from exc
        if resp.status_code >= 500:
            raise AccountServiceUnavailable(f"account service returned {resp.status_code}")
        return resp.status_code, resp.json()

    return _guarded(_do)


def get_balance(account_id: str) -> dict | None:
    """Fetch balance from the Account Service. Returns the body, or None on 404.

    Raises AccountServiceUnavailable on transport error, timeout, 5xx, or an
    OPEN circuit breaker.
    """

    def _do():
        try:
            resp = get_client().get(f"/accounts/{account_id}/balance", headers=_headers())
        except httpx.RequestError as exc:
            raise AccountServiceUnavailable(f"request error: {exc}") from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= 500:
            raise AccountServiceUnavailable(f"account service returned {resp.status_code}")
        return resp.json()

    return _guarded(_do)
