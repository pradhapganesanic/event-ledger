# Event Ledger

[![CI](https://github.com/pradhapganesanic/event-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/pradhapganesanic/event-ledger/actions/workflows/ci.yml)

Two independent microservices that process financial transaction events, built
with **Python / FastAPI**. Each service is an independently runnable process
with its **own embedded SQLite database** — they share no database or in-process
state and communicate only over REST.

> **Build status:** all functional requirements are implemented — the Gateway →
> Account Service apply call, distributed tracing, a Prometheus `/metrics`
> custom metric, the Gateway balance proxy, graceful degradation, a **circuit
> breaker** on the Account Service call, and an end-to-end integration test.
> Remaining items are optional bonuses (see [Roadmap](#roadmap)).

---

## Architecture

```
Client ──▶ Event Gateway (:8000, public)  ──REST──▶  Account Service (:8001, internal)
             owns the EVENT record                     owns ACCOUNT state + BALANCE
             gateway.db (SQLite)                        account.db (SQLite)
```

- **Event Gateway** — entry point for clients. Validates input, enforces
  idempotency, calls the Account Service to apply the transaction, and stores the
  full event in its own DB. It is authoritative for "what was submitted" and
  proxies balance queries to the Account Service.
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
| `GET` | `/accounts/{id}/balance` | Balance proxy → Account Service (`503` if it is down) |
| `GET` | `/health` | Health + DB connectivity |
| `GET` | `/metrics` | Prometheus metrics |

**Account Service (internal)**

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/accounts/{id}/transactions` | Apply a transaction (idempotent on `eventId`) |
| `GET` | `/accounts/{id}/balance` | Current balance |
| `GET` | `/accounts/{id}` | Details + recent transactions |
| `GET` | `/health` | Health + DB connectivity |
| `GET` | `/metrics` | Prometheus metrics |

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
- **Distributed tracing** — the Gateway generates a trace ID per request (or
  honours an inbound `X-Trace-Id`), propagates it to the Account Service via
  header, and both services log it. It is also echoed on responses. A single
  request produces one traceable path across both services.
- **Graceful degradation** — when the Account Service is unreachable,
  `POST /events` and balance queries return a clear `503`; event reads
  (`GET /events/{id}`, `GET /events?account=`) keep working from local data.
- **Resiliency (circuit breaker)** — the Gateway's Account Service client is
  wrapped in a circuit breaker (`gateway/app/resiliency.py`). After
  `ACCOUNT_BREAKER_THRESHOLD` (default 5) consecutive failures it trips **OPEN**
  and fails fast — rejecting calls as `503` **without** touching the Account
  Service — then probes for recovery after `ACCOUNT_BREAKER_RECOVERY_SECONDS`
  (default 10s). It composes with the per-request timeout (a timeout is one of
  the failures the breaker counts). See [why a breaker](#resiliency-choice).

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
- Account Service — **internal only** under compose (not published to the host);
  the Gateway reaches it in-network at `http://account-service:8001`.

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

Three suites: one per service (unit) plus an end-to-end integration suite. From
a venv with dev deps installed:

```bash
# Account Service (unit)
cd account-service && pip install -r requirements-dev.txt && pytest

# Event Gateway (unit) — uses a FakeAccount stand-in for the Account Service
cd gateway && pip install -r requirements-dev.txt && pytest

# End-to-end integration — starts BOTH real services as subprocesses over HTTP
cd <repo-root> && pytest integration
```

Coverage:

- **Core** — idempotency, out-of-order listing/balance, validation, health
  (both services in isolation).
- **Trace propagation** — the Gateway generates a trace ID and propagates the
  same ID to the Account Service; the Account Service echoes an inbound ID.
- **Graceful degradation** — Account Service down → `POST /events` and balance
  queries return `503` (and no orphan event is stored); event reads still work.
- **Balance proxy** — returns balance, `404` for unknown accounts, `503` when
  the Account Service is down.
- **Resiliency (circuit breaker)** — repeated failures trip the breaker OPEN and
  the Gateway stops calling the Account Service (asserted via a call counter);
  plus unit tests for OPEN → HALF_OPEN recovery, re-open on trial failure, reset.
- **Integration** — full Gateway → Account Service flow over real HTTP: apply,
  trace ID present, balance proxy, idempotent resubmit, out-of-order + debit.

### Continuous integration

`.github/workflows/ci.yml` runs on every pull request and on pushes to the
default branch:

- **`unit-tests`** — runs each service's suite with a **100% coverage gate**
  (`--cov-branch --cov-fail-under=100`). The build **fails if either line or
  branch coverage drops below 100%**, so a PR cannot merge under the threshold.
  CI reports the two figures on separate lines:

  ```
  Line coverage:   100.00%
  Branch coverage: 100.00%
  ```
- **`integration-tests`** — starts both real services and runs the `integration`
  suite over HTTP.

Reproduce the coverage gate locally (line + branch, must be 100%):

```bash
cd account-service && pytest --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=100
cd gateway         && pytest --cov=app --cov-branch --cov-report=term-missing --cov-fail-under=100
```

---

## Observability

- **Metrics** — both services expose `GET /metrics` in Prometheus text format
  (via the `prometheus-client` library), satisfying Req #4 through both an
  **endpoint** and an **observability library**. Exposed:
  - `http_requests_total{method,endpoint,status}` — request count + error rate
    (labelled by route template, not raw path, to bound cardinality)
  - `http_request_duration_seconds` — latency histogram
  - **custom domain counter** — `gateway_events_total{outcome}` (stored |
    duplicate | rejected | failed) and `account_transactions_total{outcome}`
    (applied | duplicate)
- **Structured logging** — JSON logs (`timestamp`, `level`, `service`,
  `traceId`, `logger`, `message`) on stdout for both services. The `traceId` is
  the propagated trace ID, so logs from a single request correlate across both
  services. Every transaction is also logged with an `outcome` field mirroring
  the counter labels above.
- **Health** — `GET /health` reports service status and DB connectivity
  (`503` if the DB is unreachable).

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
  through the Gateway. The Gateway therefore exposes
  `GET /accounts/{id}/balance` that proxies to the Account Service (and returns
  `503` when it is down).
- **Request timeout** — the Account Service client applies a timeout (default
  `3s`, `ACCOUNT_TIMEOUT_SECONDS`) so a slow/hung Account Service cannot block
  the Gateway. Timeouts are counted as failures by the circuit breaker below.

<a id="resiliency-choice"></a>

### Resiliency pattern choice — circuit breaker

The handout asks for **at least one** resiliency pattern and an explanation of
the choice. This project uses a **circuit breaker** (plus a request timeout).

- **Why breaker over retry:** the Account Service is a single synchronous
  dependency. When it is genuinely down, retrying just adds load to a failing
  service and drags every request out to the full timeout before failing. A
  breaker instead **fails fast** — once tripped it returns `503` immediately
  without contacting the Account Service — protecting the Gateway's threads and
  giving the dependency room to recover.
- **How it composes:** the per-request timeout bounds any single slow call; the
  breaker bounds *repeated* failure by short-circuiting. An OPEN breaker maps to
  the same `503` graceful-degradation path already used when the service is down.
- **Tunable via env:** `ACCOUNT_BREAKER_THRESHOLD` (default 5),
  `ACCOUNT_BREAKER_RECOVERY_SECONDS` (default 10).

Retry-with-backoff was intentionally *not* added: with a strictly idempotent
downstream it would be safe, but it works against fail-fast here and adds
latency during outages. It remains an easy future addition for transient blips.

---

## Roadmap

Done — all functional requirements:

- ✅ **Gateway → Account Service apply call** inside `POST /events` (call-first
  contract below)
- ✅ **Distributed tracing** — trace ID generated at the Gateway, propagated via
  header, logged in both services, echoed on responses
- ✅ **Custom metric** — Prometheus `/metrics` on both services (see Observability)
- ✅ **Graceful degradation** — `POST /events` and balance queries return `503`
  when the Account Service is down; event reads keep working
- ✅ **Balance proxy** endpoint on the Gateway
- ✅ **Resiliency** — circuit breaker on the Account Service call, with a test
  that trips it and asserts it stops calling the service
- ✅ **Integration + trace-propagation + resiliency tests** across both services

Remaining (optional bonuses):

- OTel Collector + Jaeger for trace visualization, rate limiting, and async
  fallback (queue events locally when the Account Service is down).

### Design decision — `POST /events` is "call-first, no orphan rows"

When the Account Service is down, the Gateway **rejects with `503` and stores
nothing**. It does not persist an unapplied event. This keeps a clean
invariant:

> **A Gateway event exists ⟺ a matching Account Service transaction exists.**
> The two stores mirror each other; there are no "received but not applied"
> limbo rows.

Chosen flow:

```
POST /events
1. validate
2. dedup: eventId already in Gateway store?  → return original, 200
3. call Account Service (idempotent on eventId)
     success        → store event (APPLIED), return 201
     down / breaker → return 503, store NOTHING
```

**Why this is correct on retry:** the Account Service is the idempotency
authority (unique constraint on `eventId`). If a call is applied but the
response times out, the Gateway returns `503` and stores nothing; a client
retry re-calls the Account Service, which recognises the duplicate `eventId`,
returns the already-applied transaction, and the Gateway then records it. The
system converges with no double-apply and no lost transaction — which is exactly
why `eventId` is propagated to the Account Service.

**Trade-offs accepted:** no audit of failed/attempted submissions, `GET /events`
does not surface `PENDING` events during an outage, and the async-fallback bonus
(queue-when-down) would need a separate outbox rather than reusing the events
table. None of these are required by the brief. Under this design the `status`
column is effectively always `APPLIED`; it is kept as low-cost headroom.
