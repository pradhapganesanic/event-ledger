"""Audit-stream tests — the dedicated `audit` logger records the ledger action
(TXN_APPLIED) and rejected duplicates as a separate, filterable stream."""
import logging


def _txn(event_id="e", type_="CREDIT", amount=10):
    return {
        "eventId": event_id,
        "type": type_,
        "amount": amount,
        "currency": "USD",
        "transactionTimestamp": "2026-05-15T14:02:11Z",
    }


def _audit_actions(caplog):
    return [getattr(r, "extra_fields", {}).get("action") for r in caplog.records if r.name == "audit"]


def test_audit_txn_applied(client, caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        r = client.post("/accounts/a/transactions", json=_txn("e1"))
    assert r.status_code == 201
    assert "TXN_APPLIED" in _audit_actions(caplog)


def test_audit_duplicate_rejected(client, caplog):
    client.post("/accounts/a/transactions", json=_txn("dup"))
    with caplog.at_level(logging.INFO, logger="audit"):
        client.post("/accounts/a/transactions", json=_txn("dup"))
    assert "DUPLICATE_REJECTED" in _audit_actions(caplog)


def test_audit_is_a_separate_marked_stream(client, caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        client.post("/accounts/a/transactions", json=_txn("e2"))
    audit_records = [r for r in caplog.records if r.name == "audit"]
    assert audit_records
    assert all(getattr(r, "extra_fields", {}).get("audit") is True for r in audit_records)
