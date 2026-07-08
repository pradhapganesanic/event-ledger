"""White-box tests for defensive paths (to reach 100% line + branch coverage)."""
import json
import logging
import sys
from unittest.mock import MagicMock

from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app import main
from app.database import get_db
from app.logging_config import JsonFormatter
from app.main import app
from app.schemas import TransactionIn


def test_json_formatter_plain_and_exception():
    fmt = JsonFormatter()
    plain = json.loads(fmt.format(logging.LogRecord("n", logging.INFO, "p", 1, "hi", None, None)))
    assert plain["message"] == "hi" and "exception" not in plain

    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord("n", logging.ERROR, "p", 1, "oops", None, sys.exc_info())
    with_exc = json.loads(fmt.format(rec))
    assert "exception" in with_exc


def test_apply_transaction_integrity_race_returns_existing():
    # Simulate a concurrent duplicate: pre-check misses, commit hits the unique
    # constraint, and the post-rollback lookup finds the row a rival just wrote.
    existing = MagicMock()
    existing.to_dict.return_value = {"eventId": "e", "accountId": "a"}
    db = MagicMock()
    db.scalar.side_effect = [None, existing]
    db.commit.side_effect = IntegrityError("stmt", {}, Exception("dup"))
    resp = Response()
    body = TransactionIn(eventId="e", type="CREDIT", amount=1, currency="USD", transactionTimestamp="2026-05-15T14:02:11Z")

    result = main.apply_transaction("a", body, resp, db)
    assert resp.status_code == 200
    assert result == {"eventId": "e", "accountId": "a"}
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
