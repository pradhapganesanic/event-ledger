"""Audit-stream tests — the dedicated `audit` logger emits a separate,
filterable record at each action point (notably around the Account call)."""
import logging


def _event(event_id="e", account_id="a", type_="CREDIT", amount=10):
    return {
        "eventId": event_id,
        "accountId": account_id,
        "type": type_,
        "amount": amount,
        "currency": "USD",
        "eventTimestamp": "2026-05-15T14:02:11Z",
    }


def _audit_actions(caplog):
    return [getattr(r, "extra_fields", {}).get("action") for r in caplog.records if r.name == "audit"]


def test_audit_event_applied(client, fake_account, caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        r = client.post("/events", json=_event("e1"))
    assert r.status_code == 201
    assert "EVENT_APPLIED" in _audit_actions(caplog)


def test_audit_duplicate_rejected(client, fake_account, caplog):
    client.post("/events", json=_event("dup"))
    with caplog.at_level(logging.INFO, logger="audit"):
        client.post("/events", json=_event("dup"))
    assert "DUPLICATE_REJECTED" in _audit_actions(caplog)


def test_audit_apply_failed(client, fake_account, caplog):
    fake_account.down = True
    with caplog.at_level(logging.INFO, logger="audit"):
        r = client.post("/events", json=_event("e2"))
    assert r.status_code == 503
    assert "EVENT_APPLY_FAILED" in _audit_actions(caplog)


def test_audit_validation_failed(client, caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        r = client.post("/events", json=_event("bad", amount=-1))
    assert r.status_code == 400
    assert "VALIDATION_FAILED" in _audit_actions(caplog)


def test_audit_is_a_separate_marked_stream(client, fake_account, caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        client.post("/events", json=_event("e3"))
    audit_records = [r for r in caplog.records if r.name == "audit"]
    assert audit_records
    assert all(getattr(r, "extra_fields", {}).get("audit") is True for r in audit_records)
