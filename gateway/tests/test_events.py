"""Event Gateway unit tests — service tested in isolation (no Account Service).

Covers core functionality: validation, idempotency, out-of-order listing, and
local-only reads. Cross-service concerns (apply call, tracing, resiliency,
graceful degradation) are Phase 2.
"""


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


def test_post_event_stores_and_returns_201(client):
    r = client.post("/events", json=_event("evt-1", "acct-1", "CREDIT", 150.00, metadata={"source": "batch"}))
    assert r.status_code == 201
    body = r.json()
    assert body["eventId"] == "evt-1"
    assert body["status"] == "PENDING"
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
