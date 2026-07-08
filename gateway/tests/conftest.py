"""Test fixtures for the Gateway.

Each test gets a fresh in-memory Gateway DB plus a `FakeAccount` standing in for
the Account Service. FakeAccount is a small stateful httpx.MockTransport that
mimics the Account Service contract (apply is idempotent on eventId; balance =
credits - debits), records the propagated trace ID, and can simulate an outage
via `fake_account.down = True`.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import account_client
from app.database import Base, get_db
from app.main import app
from app.tracing import TRACE_HEADER


class FakeAccount:
    """In-memory stand-in for the Account Service."""

    def __init__(self):
        self.transactions = {}  # event_id -> {accountId, type, amount, ...}
        self.last_trace_id = None
        self.down = False
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.last_trace_id = request.headers.get(TRACE_HEADER)
        if self.down:
            raise httpx.ConnectError("account service unavailable")

        parts = request.url.path.strip("/").split("/")  # accounts/{id}[/...]
        account_id = parts[1]
        headers = {TRACE_HEADER: self.last_trace_id or "-"}

        if request.method == "POST" and parts[-1] == "transactions":
            body = json.loads(request.content)
            eid = body["eventId"]
            if eid in self.transactions:
                return httpx.Response(200, json=self.transactions[eid], headers=headers)
            txn = {"eventId": eid, "accountId": account_id, **{k: body[k] for k in ("type", "amount", "currency")}}
            self.transactions[eid] = txn
            return httpx.Response(201, json=txn, headers=headers)

        if request.method == "GET" and parts[-1] == "balance":
            txns = [t for t in self.transactions.values() if t["accountId"] == account_id]
            if not txns:
                return httpx.Response(404, json={"detail": "not found"}, headers=headers)
            balance = round(
                sum(t["amount"] for t in txns if t["type"] == "CREDIT")
                - sum(t["amount"] for t in txns if t["type"] == "DEBIT"),
                2,
            )
            return httpx.Response(200, json={"accountId": account_id, "balance": balance}, headers=headers)

        return httpx.Response(404, json={"detail": "unknown"}, headers=headers)


@pytest.fixture
def fake_account():
    return FakeAccount()


@pytest.fixture
def client(fake_account):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    account_client.set_client(httpx.Client(transport=fake_account.transport, base_url="http://account-test"))

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()
    account_client.reset_client()
