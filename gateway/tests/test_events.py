"""Event Gateway tests.

Covers core functionality (validation, idempotency, out-of-order listing,
local-only reads) plus the cross-service behaviour exercised through a
FakeAccount stand-in: trace propagation, graceful degradation, and the balance
proxy. A true end-to-end test against a real Account Service lives in
`integration/`.
"""

from app.tracing import TRACE_HEADER


def _event(event_id, account_id, type_, amount, ts="2026-05-15T14:02:11Z", currency="USD", metadata=None):
    body = {
        "eventId": event_id,
        "accountId": account_id,
        "type": type_,
        "amount": amount,
        "currency": currency,
        "eventTimestamp": ts,
    }
    if metadata is not None:
        body["metadata"] = metadata
    return body


def test_post_event_applies_and_returns_201(client):
    r = client.post("/events", json=_event("evt-1", "acct-1", "CREDIT", 150.00, metadata={"source": "batch"}))
    assert r.status_code == 201
    body = r.json()
    assert body["eventId"] == "evt-1"
    assert body["status"] == "APPLIED"  # applied on the Account Service before storing
    assert body["metadata"] == {"source": "batch"}


def test_duplicate_event_is_idempotent_returns_original(client):
    first = client.post("/events", json=_event("dup", "acct-1", "CREDIT", 100))
    assert first.status_code == 201

    second = client.post("/events", json=_event("dup", "acct-1", "CREDIT", 100))
    assert second.status_code == 200
    assert second.json()["eventId"] == "dup"

    # Only one event exists for the account.
    listing = client.get("/events", params={"account": "acct-1"}).json()
    assert len(listing["events"]) == 1


def test_get_event_by_id(client):
    client.post("/events", json=_event("evt-1", "acct-1", "CREDIT", 10))
    r = client.get("/events/evt-1")
    assert r.status_code == 200
    assert r.json()["eventId"] == "evt-1"


def test_get_unknown_event_returns_404(client):
    assert client.get("/events/missing").status_code == 404


def test_list_events_ordered_by_event_timestamp(client):
    # Submit out of chronological order.
    client.post("/events", json=_event("late", "acct-1", "CREDIT", 10, ts="2026-05-15T18:00:00Z"))
    client.post("/events", json=_event("early", "acct-1", "DEBIT", 5, ts="2026-05-15T08:00:00Z"))
    client.post("/events", json=_event("mid", "acct-1", "CREDIT", 7, ts="2026-05-15T12:00:00Z"))

    events = client.get("/events", params={"account": "acct-1"}).json()["events"]
    # Listing must be chronological by eventTimestamp, not arrival order.
    assert [e["eventId"] for e in events] == ["early", "mid", "late"]


def test_list_events_filters_by_account(client):
    client.post("/events", json=_event("a", "acct-1", "CREDIT", 10))
    client.post("/events", json=_event("b", "acct-2", "CREDIT", 20))

    events = client.get("/events", params={"account": "acct-1"}).json()["events"]
    assert [e["eventId"] for e in events] == ["a"]


def test_validation_missing_required_field_returns_400(client):
    bad = _event("evt-1", "acct-1", "CREDIT", 10)
    del bad["currency"]
    r = client.post("/events", json=bad)
    assert r.status_code == 400
    assert r.json()["error"] == "validation_error"


def test_validation_non_positive_amount_returns_400(client):
    assert client.post("/events", json=_event("e", "acct-1", "CREDIT", 0)).status_code == 400
    assert client.post("/events", json=_event("e", "acct-1", "CREDIT", -1)).status_code == 400


def test_validation_unknown_type_returns_400(client):
    assert client.post("/events", json=_event("e", "acct-1", "TRANSFER", 10)).status_code == 400


def test_health_reports_db_connected(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["service"] == "event-gateway"
    assert body["database"] == "connected"


# --- Distributed tracing (Req #3) ---


def test_trace_id_generated_and_propagated(client, fake_account):
    r = client.post("/events", json=_event("e1", "acct-1", "CREDIT", 10))
    assert r.status_code == 201
    trace_id = r.headers[TRACE_HEADER]
    assert trace_id and trace_id != "-"
    # The SAME trace ID the Gateway generated was propagated to the Account Service.
    assert fake_account.last_trace_id == trace_id


def test_incoming_trace_id_is_honored_and_propagated(client, fake_account):
    r = client.post(
        "/events",
        json=_event("e2", "acct-1", "CREDIT", 10),
        headers={TRACE_HEADER: "trace-xyz"},
    )
    assert r.headers[TRACE_HEADER] == "trace-xyz"
    assert fake_account.last_trace_id == "trace-xyz"


# --- Graceful degradation (Req #6) ---


def test_post_events_returns_503_when_account_down(client, fake_account):
    fake_account.down = True
    r = client.post("/events", json=_event("e3", "acct-1", "CREDIT", 10))
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "account_service_unavailable"
    # No orphan row: nothing was stored because the apply failed.
    assert client.get("/events/e3").status_code == 404


def test_reads_still_work_when_account_down(client, fake_account):
    client.post("/events", json=_event("ok", "acct-1", "CREDIT", 10))  # applied while up
    fake_account.down = True
    assert client.get("/events/ok").status_code == 200
    assert client.get("/events", params={"account": "acct-1"}).status_code == 200


# --- Balance proxy (Req #6) ---


def test_balance_proxy_returns_balance(client):
    client.post("/events", json=_event("c1", "acct-1", "CREDIT", 100))
    client.post("/events", json=_event("d1", "acct-1", "DEBIT", 30))
    r = client.get("/accounts/acct-1/balance")
    assert r.status_code == 200
    assert r.json() == {"accountId": "acct-1", "balance": 70.0}


def test_balance_proxy_404_for_unknown_account(client):
    assert client.get("/accounts/ghost/balance").status_code == 404


def test_balance_proxy_503_when_account_down(client, fake_account):
    fake_account.down = True
    r = client.get("/accounts/acct-1/balance")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "account_service_unavailable"


# --- Resiliency: circuit breaker (Req #5, closes the Req #8 resiliency-test gap) ---


def test_circuit_breaker_opens_and_stops_calling(client, fake_account):
    fake_account.down = True
    # failure_threshold consecutive failures -> breaker trips OPEN.
    for i in range(5):
        assert client.post("/events", json=_event(f"f{i}", "a", "CREDIT", 1)).status_code == 503

    calls_before = fake_account.calls
    r = client.post("/events", json=_event("blocked", "a", "CREDIT", 1))
    assert r.status_code == 503
    # Breaker short-circuited: the Account Service was never contacted again.
    assert fake_account.calls == calls_before


def test_circuit_breaker_closed_calls_pass_through(client, fake_account):
    # Sanity: while CLOSED, calls reach the Account Service normally.
    r = client.post("/events", json=_event("ok", "a", "CREDIT", 1))
    assert r.status_code == 201
    assert fake_account.calls == 1


def test_get_db_dependency_yields_and_closes():
    # Exercise the real get_db dependency (tests otherwise override it).
    from app.database import get_db

    gen = get_db()
    session = next(gen)
    assert session is not None
    gen.close()


# --- Custom metric (Req #4): Prometheus /metrics endpoint ---


def test_metrics_endpoint_exposes_prometheus_format(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    body = r.text
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body
    assert "gateway_events_total" in body


def test_custom_event_counter_increments_by_outcome(client, fake_account):
    client.post("/events", json=_event("m1", "acct-1", "CREDIT", 10))  # stored
    client.post("/events", json=_event("m1", "acct-1", "CREDIT", 10))  # duplicate
    client.post("/events", json=_event("bad", "acct-1", "CREDIT", -1))  # rejected
    fake_account.down = True
    client.post("/events", json=_event("m2", "acct-1", "CREDIT", 10))  # failed

    body = client.get("/metrics").text
    for outcome in ("stored", "duplicate", "rejected", "failed"):
        assert f'gateway_events_total{{outcome="{outcome}"}}' in body
