"""Account Service unit tests — service tested in isolation (no Gateway)."""


def _txn(event_id, type_, amount, ts="2026-05-15T14:02:11Z", currency="USD"):
    return {
        "eventId": event_id,
        "type": type_,
        "amount": amount,
        "currency": currency,
        "transactionTimestamp": ts,
    }


def test_apply_transaction_returns_201_and_updates_balance(client):
    r = client.post("/accounts/acct-1/transactions", json=_txn("evt-1", "CREDIT", 150.00))
    assert r.status_code == 201
    assert r.json()["eventId"] == "evt-1"

    b = client.get("/accounts/acct-1/balance")
    assert b.status_code == 200
    assert b.json() == {"accountId": "acct-1", "balance": 150.00}


def test_balance_is_credits_minus_debits(client):
    client.post("/accounts/acct-1/transactions", json=_txn("e1", "CREDIT", 200))
    client.post("/accounts/acct-1/transactions", json=_txn("e2", "DEBIT", 75))
    client.post("/accounts/acct-1/transactions", json=_txn("e3", "CREDIT", 25))

    assert client.get("/accounts/acct-1/balance").json()["balance"] == 150.00


def test_idempotent_duplicate_event_does_not_double_apply(client):
    first = client.post("/accounts/acct-1/transactions", json=_txn("dup", "CREDIT", 100))
    assert first.status_code == 201

    # Same eventId submitted again -> 200, returns original, balance unchanged.
    second = client.post("/accounts/acct-1/transactions", json=_txn("dup", "CREDIT", 100))
    assert second.status_code == 200
    assert second.json()["eventId"] == "dup"

    assert client.get("/accounts/acct-1/balance").json()["balance"] == 100.00


def test_out_of_order_arrival_yields_correct_balance(client):
    # Later timestamp applied first, earlier timestamp second.
    client.post("/accounts/acct-1/transactions", json=_txn("late", "CREDIT", 100, ts="2026-05-15T18:00:00Z"))
    client.post("/accounts/acct-1/transactions", json=_txn("early", "DEBIT", 40, ts="2026-05-15T09:00:00Z"))

    # Balance is order-independent (a sum), so it must be correct either way.
    assert client.get("/accounts/acct-1/balance").json()["balance"] == 60.00


def test_get_account_details_recent_transactions_ordered_desc(client):
    client.post("/accounts/acct-1/transactions", json=_txn("e1", "CREDIT", 10, ts="2026-05-15T09:00:00Z"))
    client.post("/accounts/acct-1/transactions", json=_txn("e2", "CREDIT", 20, ts="2026-05-15T11:00:00Z"))

    r = client.get("/accounts/acct-1")
    assert r.status_code == 200
    body = r.json()
    assert body["balance"] == 30.00
    # Most recent first.
    assert [t["eventId"] for t in body["recentTransactions"]] == ["e2", "e1"]


def test_balance_unknown_account_returns_404(client):
    r = client.get("/accounts/nope/balance")
    assert r.status_code == 404


def test_validation_rejects_bad_type(client):
    r = client.post("/accounts/acct-1/transactions", json=_txn("e", "TRANSFER", 10))
    assert r.status_code == 422


def test_validation_rejects_non_positive_amount(client):
    r = client.post("/accounts/acct-1/transactions", json=_txn("e", "CREDIT", 0))
    assert r.status_code == 422
    r = client.post("/accounts/acct-1/transactions", json=_txn("e", "CREDIT", -5))
    assert r.status_code == 422


def test_health_reports_db_connected(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "account-service"
    assert body["database"] == "connected"


def test_metrics_exposes_request_counts(client):
    client.post("/accounts/acct-1/transactions", json=_txn("e1", "CREDIT", 10))
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.json()["service"] == "account-service"
    assert any("transactions" in k for k in r.json()["requestsByEndpoint"])
