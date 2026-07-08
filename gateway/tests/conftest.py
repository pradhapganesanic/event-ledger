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
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import account_client, otel
from app.database import Base, get_db
from app.main import app
from app.tracing import TRACE_HEADER

# Capture OTel spans in memory (no Collector/Jaeger needed for tests).
_memory_exporter = InMemorySpanExporter()
otel.provider.add_span_processor(SimpleSpanProcessor(_memory_exporter))


@pytest.fixture
def spans() -> InMemorySpanExporter:
    _memory_exporter.clear()
    return _memory_exporter


class FakeAccount:
    """In-memory stand-in for the Account Service."""

    def __init__(self):
        self.transactions = {}  # event_id -> {accountId, type, amount, ...}
        self.last_trace_id = None
        self.last_traceparent = None
        self.down = False
        self.calls = 0  # number of times the service was actually contacted
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        self.last_trace_id = request.headers.get(TRACE_HEADER)
        self.last_traceparent = request.headers.get("traceparent")
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
    # The breaker is a process-wide singleton; reset it so state can't leak
    # between tests and make them order-dependent.
    account_client.account_breaker.reset()

    with TestClient(app) as c:
        yield c

    app.dependency_overrides.clear()
    account_client.reset_client()
    account_client.account_breaker.reset()
