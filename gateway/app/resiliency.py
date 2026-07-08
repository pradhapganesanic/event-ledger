"""Circuit breaker for the Gateway's calls to the Account Service (Req #5).

Why a circuit breaker (vs. plain retry): the Account Service is a single
synchronous dependency. When it is genuinely down, retrying just piles load onto
a failing service and slows every request down to the timeout. A breaker instead
*fails fast* — after N consecutive failures it trips OPEN and rejects calls
immediately (mapped to a 503 by the Gateway) without touching the Account
Service, then probes for recovery after a cooldown. It composes with the request
timeout already in the client (timeout produces the failures the breaker counts).

States: CLOSED (normal) -> OPEN (fail fast) -> HALF_OPEN (one trial) -> CLOSED.
Thread-safe: the Gateway runs sync endpoints in a threadpool.
"""
import threading
import time
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Rejected without calling the dependency — the breaker is OPEN."""


class CircuitBreaker:
    def __init__(
        self,
        name,
        failure_threshold=5,
        recovery_timeout=10.0,
        failure_exceptions=(Exception,),
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_exceptions = failure_exceptions
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    def reset(self) -> None:
        """Return the breaker to CLOSED (used to isolate tests)."""
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._opened_at = 0.0

    def call(self, fn, *args, **kwargs):
        with self._lock:
            if self._state is CircuitState.OPEN:
                if time.monotonic() - self._opened_at >= self.recovery_timeout:
                    self._state = CircuitState.HALF_OPEN
                else:
                    raise CircuitOpenError(f"circuit '{self.name}' is OPEN")
        try:
            result = fn(*args, **kwargs)
        except self.failure_exceptions:
            with self._lock:
                self._failures += 1
                if self._state is CircuitState.HALF_OPEN or self._failures >= self.failure_threshold:
                    self._state, self._opened_at = CircuitState.OPEN, time.monotonic()
            raise
        with self._lock:
            self._failures, self._state = 0, CircuitState.CLOSED
        return result


# Registry seam — one breaker per dependency; today there's exactly one.
_registry: dict[str, CircuitBreaker] = {}


def get_breaker(name, **kw) -> CircuitBreaker:
    if name not in _registry:
        _registry[name] = CircuitBreaker(name, **kw)
    return _registry[name]
