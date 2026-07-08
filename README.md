# Event Ledger

Two independent microservices that process financial transaction events, built
with **Python / FastAPI**. Each service is an independently runnable process
with its **own embedded SQLite database** — they share no database or in-process
state and communicate only over REST.

> **Build status — Phase 1 (current):** each service is fully built and tested
> in isolation (own DB, tables, endpoints, per-service unit tests).
> **Phase 2 (planned):** the cross-service concerns — the Gateway → Account
> Service apply call, trace-ID propagation, a resiliency pattern, graceful
> degradation, and an end-to-end integration test. See [Roadmap](#roadmap).

---

## Architecture

```
Client ──▶ Event Gateway (:8000, public)  ──REST──▶  Account Service (:8001, internal)
             owns the EVENT record                     owns ACCOUNT state + BALANCE
             gateway.db (SQLite)                        account.db (SQLite)
```

- **Event Gateway** — entry point for clients. Validates input, enforces
  idempotency, and stores the full event in its own DB. It is authoritative for
  "what was submitted." (In Phase 2 it also calls the Account Service to apply
  the transaction.)
- **Account Service** — internal, not exposed to clients. Owns the ledger:
  stores applied transactions and computes balance = Σ CREDIT − Σ DEBIT. It is
  authoritative for "what actually landed in the account."

The same `amount`/`type`/`currency` fields live in both databases by design —
each service keeps its own copy for its own purpose, so both keep working
independently. This intentional duplication is **not** a normalization smell;
across a service boundary each service must be self-sufficient.

### API contracts

**Event Gateway (public)**

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/events` | Submit a transaction event |
| `GET` | `/events/{id}` | Retrieve a single event |
| `GET` | `/events?account={id}` | List an account's events, ordered by `eventTimestamp` |
| `GET` | `/health` | Health + DB connectivity |
| `GET` | `/metrics` | Per-endpoint request/error counts |

**Account Service (internal)**

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/accounts/{id}/transactions` | Apply a transaction (idempotent on `eventId`) |
| `GET` | `/accounts/{id}/balance` | Current balance |
| `GET` | `/accounts/{id}` | Details + recent transactions |
| `GET` | `/health` | Health + DB connectivity |
| `GET` | `/metrics` | Per-endpoint request/error counts |

---

## Core behavior

- **Idempotency** — `eventId` is the primary key in the Gateway and a unique key
  in the Account Service. A repeated `eventId` returns the original record with
  `200` and never double-applies. A pre-check handles the common case; the
  unique constraint is the race-safe guarantee.
- **Out-of-order tolerance** — event listings are ordered by `eventTimestamp`
  (not arrival order); balance is a sum, so it is correct regardless of order.
- **Validation** — missing fields, non-positive amounts, and unknown types are
  rejected. The Gateway returns `400` with clear messages.

---

## Prerequisites

- Python 3.11+ (developed on 3.14)
- Docker + Docker Compose (optional, for the containerized run)

---

## Run — Docker Compose (preferred)

```bash
docker compose up --build
```

- Gateway → http://localhost:8000  (docs at `/docs`)
- Account Service → http://localhost:8001  (docs at `/docs`)

## Run — locally without Docker

Each service is a separate process. Use two terminals.

```bash
# Terminal 1 — Account Service on :8001
cd account-service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8001
```

```bash
# Terminal 2 — Event Gateway on :8000
cd gateway
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000
```

### Quick smoke test

```bash
# Submit an event to the Gateway
curl -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{"eventId":"evt-001","accountId":"acct-123","type":"CREDIT","amount":150.00,"currency":"USD","eventTimestamp":"2026-05-15T14:02:11Z"}'

# Apply a transaction directly to the Account Service
curl -X POST http://localhost:8001/accounts/acct-123/transactions \
  -H 'Content-Type: application/json' \
  -d '{"eventId":"evt-001","type":"CREDIT","amount":150.00,"currency":"USD","transactionTimestamp":"2026-05-15T14:02:11Z"}'

curl http://localhost:8001/accounts/acct-123/balance
```

---

## Tests

Each service has its own suite. From a venv with dev deps installed:

```bash
# Account Service
cd account-service
pip install -r requirements-dev.txt
pytest

# Event Gateway
cd gateway
pip install -r requirements-dev.txt
pytest
```

Covered in Phase 1: idempotency, out-of-order listing/balance, validation,
health, and metrics — for each service in isolation.

---

## Observability

- **Structured logging** — JSON logs (`timestamp`, `level`, `service`,
  `message`) on stdout for both services.
- **Health** — `GET /health` reports service status and DB connectivity
  (`503` if the DB is unreachable).
- **Custom metric** — `GET /metrics` exposes per-endpoint request and error
  counts.

---

## Design notes & assumptions

- **Money** is stored as `NUMERIC(18,2)` and returned as a rounded float for
  simplicity; production would use integer minor units or `Decimal` end-to-end.
- **Balance** assumes a single currency per account (the handout models balance
  as one number). Mixed-currency accounts would need per-currency balances.
- **Unknown account** balance/detail queries return `404` (an account "exists"
  once it has ≥1 transaction) rather than a synthetic zero, so unknown is
  distinguishable from empty.
- **Balance proxy on the Gateway** — the handout lists a balance endpoint only
  on the (internal) Account Service, but external clients can reach it only
  through the Gateway. A Gateway balance proxy is therefore planned for Phase 2.

---

## Roadmap

Phase 2 (cross-service concerns, deferred by design):

- Gateway → Account Service apply call inside `POST /events`
  (status `PENDING` → `APPLIED`/`FAILED`)
- **Distributed tracing** — generate a trace ID at the Gateway, propagate via
  HTTP header, log it in both services
- **Resiliency** — timeout + retry with backoff and/or a circuit breaker on the
  Account Service call
- **Graceful degradation** — `POST /events` returns `503` when the Account
  Service is down; event reads keep working; balance proxy returns a clear error
- **Balance proxy** endpoint on the Gateway
- **Integration + resiliency tests** across both services
