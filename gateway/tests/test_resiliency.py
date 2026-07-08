"""Unit tests for the CircuitBreaker itself (Req #5).

These cover the state transitions the HTTP-level test doesn't: OPEN short-circuit,
HALF_OPEN recovery on success, HALF_OPEN failure re-opening, and reset.
"""
import time

import pytest

from app.resiliency import CircuitBreaker, CircuitOpenError, CircuitState, get_breaker


class Boom(Exception):
    pass


def _fail():
    raise Boom()


def test_opens_after_threshold_then_short_circuits():
    cb = CircuitBreaker("t", failure_threshold=2, recovery_timeout=60, failure_exceptions=(Boom,))
    for _ in range(2):
        with pytest.raises(Boom):
            cb.call(_fail)
    assert cb.state is CircuitState.OPEN

    calls = {"n": 0}

    def spy():
        calls["n"] += 1
        return "ok"

    with pytest.raises(CircuitOpenError):
        cb.call(spy)
    assert calls["n"] == 0  # never invoked while OPEN


def test_success_in_closed_resets_failure_count():
    cb = CircuitBreaker("t", failure_threshold=3, recovery_timeout=60, failure_exceptions=(Boom,))
    with pytest.raises(Boom):
        cb.call(_fail)
    assert cb.call(lambda: "ok") == "ok"  # success resets
    assert cb.state is CircuitState.CLOSED


def test_half_open_recovers_on_success():
    cb = CircuitBreaker("t", failure_threshold=1, recovery_timeout=0.01, failure_exceptions=(Boom,))
    with pytest.raises(Boom):
        cb.call(_fail)
    assert cb.state is CircuitState.OPEN
    time.sleep(0.02)  # recovery window elapses -> next call is a HALF_OPEN trial
    assert cb.call(lambda: "ok") == "ok"
    assert cb.state is CircuitState.CLOSED


def test_half_open_failure_reopens():
    cb = CircuitBreaker("t", failure_threshold=1, recovery_timeout=0.01, failure_exceptions=(Boom,))
    with pytest.raises(Boom):
        cb.call(_fail)
    time.sleep(0.02)
    with pytest.raises(Boom):  # HALF_OPEN trial fails
        cb.call(_fail)
    assert cb.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):  # immediately fails fast again
        cb.call(lambda: "ok")


def test_reset_returns_to_closed():
    cb = CircuitBreaker("t", failure_threshold=1, recovery_timeout=60, failure_exceptions=(Boom,))
    with pytest.raises(Boom):
        cb.call(_fail)
    assert cb.state is CircuitState.OPEN
    cb.reset()
    assert cb.state is CircuitState.CLOSED
    assert cb.call(lambda: "ok") == "ok"


def test_get_breaker_returns_singleton_per_name():
    assert get_breaker("dep-x") is get_breaker("dep-x")
    assert get_breaker("dep-x") is not get_breaker("dep-y")
