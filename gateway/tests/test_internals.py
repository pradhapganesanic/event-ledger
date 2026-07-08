"""White-box tests for defensive paths (to reach 100% line + branch coverage)."""
import json
import logging
import sys
from unittest.mock import MagicMock

import httpx
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app import account_client, main
from app.database import get_db
from app.logging_config import JsonFormatter
from app.main import app
from app.schemas import EventIn


def test_json_formatter_plain_and_exception():
    fmt = JsonFormatter()
    plain = json.loads(fmt.format(logging.LogRecord("n", logging.INFO, "p", 1, "hi", None, None)))
    assert plain["message"] == "hi" and "exception" not in plain

    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord("n", logging.ERROR, "p", 1, "oops", None, sys.exc_info())
    assert "exception" in json.loads(fmt.format(rec))


def test_create_event_integrity_race_returns_original(monkeypatch):
    monkeypatch.setattr(account_client, "apply_transaction", lambda *a, **k: (201, {}))
    existing = MagicMock()
    existing.to_dict.return_value = {"eventId": "e", "status": "APPLIED"}
    db = MagicMock()
    db.get.side_effect = [None, existing]  # pre-check miss, then rival's row
    db.commit.side_effect = IntegrityError("stmt", {}, Exception("dup"))
    resp = Response()
    body = EventIn(eventId="e", accountId="a", type="CREDIT", amount=1, currency="USD", eventTimestamp="2026-05-15T14:02:11Z")

    result = main.create_event(body, resp, db)
    assert resp.status_code == 200
    assert result == {"eventId": "e", "status": "APPLIED"}
    db.rollback.assert_called_once()


def test_health_degraded_when_db_unavailable():
    class BadSession:
        def execute(self, *a, **k):
            raise RuntimeError("db down")

        def close(self):
            pass

    def bad_db():
        yield BadSession()

    app.dependency_overrides[get_db] = bad_db
    try:
        with TestClient(app) as c:
            r = c.get("/health")
    finally:
        app.dependency_overrides.clear()
    assert r.status_code == 503
    assert r.json()["status"] == "degraded"
    assert r.json()["database"] == "disconnected"


def test_get_client_creates_default_singleton():
    account_client.reset_client()
    c = account_client.get_client()
    assert c is account_client.get_client()  # cached singleton
    account_client.reset_client()


def _status_client(status: int) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda req: httpx.Response(status, json={"detail": "boom"})),
        base_url="http://account-test",
    )


def test_apply_5xx_maps_to_503(client):
    account_client.set_client(_status_client(500))
    account_client.account_breaker.reset()
    r = client.post(
        "/events",
        json={"eventId": "e", "accountId": "a", "type": "CREDIT", "amount": 1, "currency": "USD", "eventTimestamp": "2026-05-15T14:02:11Z"},
    )
    assert r.status_code == 503


def test_balance_5xx_maps_to_503(client):
    account_client.set_client(_status_client(500))
    account_client.account_breaker.reset()
    assert client.get("/accounts/a/balance").status_code == 503
