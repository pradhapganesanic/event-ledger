"""End-to-end integration test (Req #8).

Starts BOTH real services as independent subprocesses (each with its own SQLite
database) and drives them over real HTTP through the Gateway, exercising the
full Gateway -> Account Service flow: apply, trace propagation, balance proxy,
and idempotency.

Run from the repo root:  pytest integration
"""
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACE_HEADER = "X-Trace-Id"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_healthy(base_url: str, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise RuntimeError(f"service at {base_url} did not become healthy in {timeout}s")


def _start(service_dir: str, port: int, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=os.path.join(ROOT, service_dir),
        env=env,
    )


@pytest.fixture(scope="module")
def services():
    tmp = tempfile.mkdtemp(prefix="event-ledger-it-")
    acct_port, gw_port = _free_port(), _free_port()
    acct_url, gw_url = f"http://127.0.0.1:{acct_port}", f"http://127.0.0.1:{gw_port}"

    acct_env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp}/account.db"}
    gw_env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{tmp}/gateway.db",
        "ACCOUNT_SERVICE_URL": acct_url,
    }

    acct = _start("account-service", acct_port, acct_env)
    gw = _start("gateway", gw_port, gw_env)
    try:
        _wait_healthy(acct_url)
        _wait_healthy(gw_url)
        yield gw_url, acct_url
    finally:
        for proc in (gw, acct):
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def _event(event_id, account_id, type_, amount, ts="2026-05-15T14:02:11Z"):
    return {
        "eventId": event_id,
        "accountId": account_id,
        "type": type_,
        "amount": amount,
        "currency": "USD",
        "eventTimestamp": ts,
    }


def test_full_flow_gateway_to_account(services):
    gw, acct = services

    # 1. Submit an event -> applied downstream, stored APPLIED, trace ID present.
    r = httpx.post(f"{gw}/events", json=_event("evt-001", "acct-123", "CREDIT", 150.00))
    assert r.status_code == 201
    assert r.json()["status"] == "APPLIED"
    assert r.headers.get(TRACE_HEADER)

    # 2. Balance via the Gateway proxy reflects the applied transaction.
    b = httpx.get(f"{gw}/accounts/acct-123/balance")
    assert b.status_code == 200
    assert b.json() == {"accountId": "acct-123", "balance": 150.0}

    # 3. The Account Service (queried directly) agrees — real cross-service state.
    ab = httpx.get(f"{acct}/accounts/acct-123/balance")
    assert ab.json()["balance"] == 150.0


def test_idempotent_resubmit_does_not_double_apply(services):
    gw, _ = services

    httpx.post(f"{gw}/events", json=_event("evt-dup", "acct-dup", "CREDIT", 100.00))
    second = httpx.post(f"{gw}/events", json=_event("evt-dup", "acct-dup", "CREDIT", 100.00))
    assert second.status_code == 200  # idempotent

    assert httpx.get(f"{gw}/accounts/acct-dup/balance").json()["balance"] == 100.0


def test_out_of_order_and_debit(services):
    gw, _ = services

    httpx.post(f"{gw}/events", json=_event("late", "acct-ooo", "CREDIT", 100, ts="2026-05-15T18:00:00Z"))
    httpx.post(f"{gw}/events", json=_event("early", "acct-ooo", "DEBIT", 40, ts="2026-05-15T09:00:00Z"))

    # Balance is order-independent.
    assert httpx.get(f"{gw}/accounts/acct-ooo/balance").json()["balance"] == 60.0
    # Listing is chronological by eventTimestamp.
    events = httpx.get(f"{gw}/events", params={"account": "acct-ooo"}).json()["events"]
    assert [e["eventId"] for e in events] == ["early", "late"]
