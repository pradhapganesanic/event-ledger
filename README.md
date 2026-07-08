# Event Ledger

[![CI](https://github.com/pradhapganesanic/event-ledger/actions/workflows/ci.yml/badge.svg)](https://github.com/pradhapganesanic/event-ledger/actions/workflows/ci.yml)

An **Event Ledger** built as **two independent microservices** (Python /
FastAPI) that ingest financial transaction events, apply them to account
balances, and stay correct under **duplicate** and **out-of-order** delivery —
with distributed tracing, metrics, a resiliency pattern, and graceful
degradation when a service is down.

Each service is an independently runnable process with its **own embedded
SQLite database** — they share no database or in-process state and communicate
only over synchronous REST.

> **Status:** all functional requirements implemented, plus bonuses (Prometheus
> metrics endpoint, OpenTelemetry → Jaeger tracing). CI enforces **100% line +
> branch** test coverage. See [Requirements coverage](#11-requirements-coverage).

---

## Table of contents

Read top-to-bottom: high-level architecture first, then each topic in detail.

1. [Architecture (high level)](#1-architecture-high-level)
2. [Components & data ownership](#2-components--data-ownership)
3. [API contracts](#3-api-contracts)
4. [Request flow — `POST /events`](#4-request-flow--post-events)
5. [Core behavior](#5-core-behavior)
6. [Resiliency & graceful degradation](#6-resiliency--graceful-degradation)
7. [Observability (tracing, metrics, logging)](#7-observability)
8. [Setup & prerequisites](#8-setup--prerequisites)
9. [Run the app](#9-run-the-app)
10. [Tests & CI](#10-tests--ci)
11. [Requirements coverage](#11-requirements-coverage)
12. [Design notes & assumptions](#12-design-notes--assumptions)
13. [Roadmap](#13-roadmap)

---

## 1. Architecture (high level)

Two services plus a tracing backend, all orchestrated by Docker Compose. The
Gateway is the only public entry point; the Account Service is internal.

```
                     ┌──────────────────────────── docker compose ─────────────────────────────┐
                     │                                                                           │
  Browser / Client   │    ┌─────────────────┐        sync REST        ┌──────────────────┐      │
      │  HTTP :8000  │    │  Event Gateway  │ ──── apply / balance ──▶ │  Account Service │      │
      └──────────────┼───▶│   (public)      │                         │   (internal)     │      │
                     │    │   :8000         │ ◀──── 201 / 200 / 5xx ── │   :8001          │      │
                     │    │   gateway.db    │                         │   account.db     │      │
                     │    └───────┬─────────┘                         └────────┬─────────┘      │
                     │            │  OTLP spans (async)     OTLP spans (async)  │                │
                     │            └───────────────┬───────────────────────────┘                │
                     │                            ▼                                              │
                     │                 ┌────────────────────────────┐                           │
                     │                 │   Jaeger all-in-one         │  OTLP ingest + in-memory  │
                     │                 │   :16686 (UI) / :4317 (OTLP)│  store + trace UI         │
                     │                 └────────────────────────────┘                           │
                     └───────────────────────────────┬───────────────────────────────────────────┘
                                                      ▲
                                     Developer → Jaeger UI (http://localhost:16686)
```

- **Event Gateway** — public-facing edge. Validates input, enforces idempotency,
  calls the Account Service to apply transactions, stores the event, and proxies
  balance queries. Authoritative for **"what was submitted."**
- **Account Service** — internal ledger. Applies transactions idempotently and
  computes balance. Authoritative for **"what actually landed in the account."**
- **Jaeger all-in-one** — receives OTLP spans from both services and renders the
  end-to-end trace. (No separate OTel Collector: a single trace backend ingests
  OTLP directly.)

---

## 2. Components & data ownership

| | **Event Gateway** | **Account Service** |
|---|---|---|
| Exposure | Public (`:8000`) | Internal only (`:8001`, not host-published) |
| Owns | the **event** record (submission log) | the **ledger** (applied transactions, balance) |
| DB | `gateway.db` (SQLite) | `account.db` (SQLite) |
| Idempotency key | `eventId` (primary key) | `eventId` (unique constraint) |
| Authoritative for | "what was submitted" | "what actually landed / balance" |

The same `amount`/`type`/`currency` fields live in **both** databases by design —
each service keeps its own copy for its own purpose, so each stays functional
independently. Across a service boundary this duplication is correct, **not** a
normalization smell.

---

## 3. API contracts

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

## 4. Request flow — `POST /events`

The Gateway uses a **"call-first, no orphan rows"** contract: it applies the
transaction on the Account Service **before** persisting locally.

```
Client            Event Gateway                              Account Service
  │  POST /events    │                                             │
  │ ────────────────▶│                                             │
  │                  │ 1. validate            → 400 on bad input   │
  │                  │ 2. dedup on eventId    → 200 (original) if seen
  │                  │ 3. [SERVER span starts; trace id in logs]   │
  │                  │ 4. apply  [CLIENT span + traceparent]       │
  │                  │ ───────────────────────────────────────────▶│ continue trace
  │                  │                                             │ 5. idempotent
  │                  │                                             │    apply (unique
  │                  │◀──────────────── 201 / 200 (dup) ───────────│    eventId)
  │                  │ 6. store event as APPLIED                   │
  │◀──── 201 ────────│                                             │
  │                  │ (down / breaker OPEN → 503, store NOTHING)  │
```

**Invariant:** a Gateway event exists **⟺** a matching Account transaction
exists — no "received but not applied" limbo rows. Retries after a `503` are
safe because the Account Service is idempotent on `eventId` (a timed-out-but-
applied call is recognized as a duplicate on retry). See
[Design notes](#12-design-notes--assumptions) for the full rationale and
trade-offs.

---

## 5. Core behavior

- **Idempotency** — `eventId` is the primary key (Gateway) / unique key
  (Account). A repeat returns the original with `200` and never double-applies. A
  cheap pre-check handles the common case; the unique constraint is the race-safe
  guarantee.
- **Out-of-order tolerance** — event listings are ordered by `eventTimestamp`
  (not arrival order); balance is a sum, so it is correct regardless of order.
- **Balance** — net = Σ CREDIT − Σ DEBIT, computed by the Account Service from
  applied transactions only.
- **Validation** — missing fields, non-positive amounts, and unknown types are
  rejected; the Gateway returns `400` with clear, field-level messages.

---

## 6. Resiliency & graceful degradation

The Gateway wraps its Account Service call in a **circuit breaker**
(`gateway/app/resiliency.py`) plus a **request timeout**.

```
                       failures < threshold (reset on success)
                        ┌──────────────────────────────────┐
                        │                                   │
                   ┌────┴─────┐   N consecutive fails   ┌───▼────┐
   normal calls ──▶│  CLOSED  │ ──────────────────────▶│  OPEN  │  fail fast:
                   │ (pass    │                         │ (reject│  return 503,
                   │  through)│◀──── trial succeeds ────│  as 503)│ DON'T call svc
                   └──────────┘                         └───┬────┘
                        ▲                                   │ after recovery_timeout
                        │        ┌───────────┐              │
                        └────────│ HALF_OPEN │◀─────────────┘
                     trial OK    │ (1 probe) │   trial fails → back to OPEN
                                 └───────────┘
```

### Why a circuit breaker (Req #5 explanation)

The handout asks for **at least one** resiliency pattern and an explanation. We
chose a **circuit breaker** because it targets the highest-value failure mode:

- **Sustained outage** → the breaker **fails fast** after N failures, returning
  `503` instantly *without* hammering a down service, protecting the Gateway's
  threads and giving the dependency room to recover. (Plain retry would *add*
  load to a failing service and drag every request out to the timeout.)
- **Composes with the timeout** — the timeout bounds any single slow call; a
  timeout counts as one of the failures the breaker tracks.
- **Tunable via env** — `ACCOUNT_BREAKER_THRESHOLD` (default 5),
  `ACCOUNT_BREAKER_RECOVERY_SECONDS` (default 10),
  `ACCOUNT_TIMEOUT_SECONDS` (default 3).

Retry-with-backoff (a *bonus*, and one of the three Req #5 options we did not
pick) was intentionally omitted: the breaker covers sustained failures; retry
addresses *transient* blips and would work against fail-fast here. It is a safe
future addition thanks to the idempotent downstream.

### Graceful degradation (when the Account Service is unavailable)

| Endpoint | Behavior | Why |
|---|---|---|
| `POST /events` | `503` (clear error), stores nothing | can't apply → no orphan row |
| `GET /accounts/{id}/balance` | `503` (clear error) | balance lives in the Account Service |
| `GET /events/{id}`, `GET /events?account=` | **still work** | served from the Gateway's local DB |

---

## 7. Observability

Three signals, each to its natural destination:

```
                        ┌─ Traces ──▶ OTLP (async push) ──▶ Jaeger UI  (:16686)
 Gateway / Account ─────┼─ Metrics ─▶ GET /metrics  ◀── scrape (pull) ── Prometheus
 (instrumented apps)    └─ Logs ────▶ stdout (structured JSON, trace-correlated)
```

- **Distributed tracing (OpenTelemetry + a lightweight header)**
  - Both services are auto-instrumented: each request → a **SERVER span**; the
    Gateway's account call → a **CLIENT span** that injects a W3C `traceparent`,
    so the Account Service **continues the same trace**.
  - Spans export via **OTLP** to **Jaeger** when `OTEL_EXPORTER_OTLP_ENDPOINT` is
    set (it is, under Compose). Export is **asynchronous** (BatchSpanProcessor) —
    no added request latency.
  - A simple `X-Trace-Id` header is also generated at the Gateway, propagated,
    logged in both services, and echoed on responses — satisfying the minimum
    tracing requirement even without the OTel stack, and used for log
    correlation.
  - **View it:** `docker compose up`, submit an event, open **Jaeger →
    http://localhost:16686**, search service `event-gateway`, open a trace to see
    the Gateway → Account waterfall.
- **Metrics** — both services expose `GET /metrics` in Prometheus text format
  (via `prometheus-client`):
  - `http_requests_total{method,endpoint,status}` — request count + error rate
  - `http_request_duration_seconds` — latency histogram
  - **custom domain counter** — `gateway_events_total{outcome}` (stored |
    duplicate | rejected | failed), `account_transactions_total{outcome}`
    (applied | duplicate)
- **Structured logging** — JSON logs (`timestamp`, `level`, `service`, `traceId`,
  `logger`, `message`) on stdout. `traceId` correlates a single request across
  both services; every transaction is logged with an `outcome` matching the
  metric labels.
- **Health** — `GET /health` on both services reports status + DB connectivity
  (`503` if the DB is unreachable).

---

## 8. Setup & prerequisites

- **Python 3.11+** (developed on 3.14) — for running/tests without Docker.
- **Docker Desktop** — for the containerized one-click run (optional but
  recommended). Install on macOS with `brew install --cask docker-desktop`, then
  launch it once (`open -a Docker`) and wait for the whale icon to go steady.

Dependencies are per-service `requirements.txt` (runtime) and
`requirements-dev.txt` (adds `pytest`, `pytest-cov`). No global install needed —
each service uses its own venv (or its container).

---

## 9. Run the app

### Docker Compose (preferred) — one click

```bash
make run          # build + start + wait for health + smoke test   (or: ./run.sh)
```

Other targets: `make up` (start detached), `make smoke`, `make logs`,
`make down` (stop + remove volumes), `make test` (all suites, no Docker),
`make help`.

- Gateway → http://localhost:8000  (API docs at `/docs`)
- Account Service — **internal only** under Compose; the Gateway reaches it at
  `http://account-service:8001`
- Jaeger UI → http://localhost:16686

### Locally without Docker (two terminals)

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
uvicorn app.main:app --port 8000     # ACCOUNT_SERVICE_URL defaults to :8001
```

Smoke it:

```bash
curl -X POST http://localhost:8000/events -H 'Content-Type: application/json' \
  -d '{"eventId":"evt-001","accountId":"acct-123","type":"CREDIT","amount":150.00,"currency":"USD","eventTimestamp":"2026-05-15T14:02:11Z"}'
curl http://localhost:8000/accounts/acct-123/balance   # → {"accountId":"acct-123","balance":150.0}
```

---

## 10. Tests & CI

Three suites — two per-service (unit) plus an end-to-end integration suite:

```bash
cd account-service && pip install -r requirements-dev.txt && pytest   # unit
cd gateway         && pip install -r requirements-dev.txt && pytest   # unit (FakeAccount stand-in)
pytest integration                                                    # starts BOTH real services over HTTP
# or simply:
make test                                                             # all three, with the coverage gate
```

**What's covered:** idempotency, out-of-order, balance, validation, health;
trace propagation (Gateway↔Account); graceful degradation (`503`, no orphan row,
reads still work); balance proxy (`200`/`404`/`503`); **circuit breaker** (trips
OPEN and stops calling; HALF_OPEN recovery/reopen/reset); **OpenTelemetry** spans
(server + client in one trace, `traceparent` propagation) via an in-memory
exporter (no Jaeger needed); and full **integration** over real HTTP.

**CI** (`.github/workflows/ci.yml`, runs on every PR and on pushes to the default
branch):

- **`unit-tests`** — each service with a **100% coverage gate**
  (`--cov-branch --cov-fail-under=100`); a PR cannot merge below 100%. Line and
  branch coverage are reported on two separate lines:
  ```
  Line coverage:   100.00%
  Branch coverage: 100.00%
  ```
- **`integration-tests`** — starts both real services and runs the integration
  suite over HTTP.

`main` is branch-protected: these checks are required to merge.

---

## 11. Requirements coverage

Mapping of each handout requirement to where it lives (this section also answers
**Req #9 — README**).

| # | Requirement | Where it's implemented |
|---|---|---|
| 1 | Idempotency, out-of-order, balance, validation | [§5 Core behavior](#5-core-behavior) · `models.py`, `main.py`, `schemas.py` |
| 2 | Service separation (own DB, no shared state, clear contracts) | [§1](#1-architecture-high-level)–[§3](#3-api-contracts) · separate `gateway/` & `account-service/` |
| 3 | Distributed tracing (trace ID generated, propagated, logged) | [§7 Observability](#7-observability) · `tracing.py`, `otel.py` |
| 4 | Observability (structured logs, health, ≥1 custom metric) | [§7 Observability](#7-observability) · `logging_config.py`, `metrics.py` |
| 5 | Resiliency pattern + explanation | [§6 Resiliency](#6-resiliency--graceful-degradation) · `resiliency.py` |
| 6 | Graceful degradation | [§6](#6-resiliency--graceful-degradation) · `main.py`, `account_client.py` |
| 7 | Docker Compose (or manual instructions) | [§9 Run](#9-run-the-app) · `docker-compose.yml`, `Makefile`, `run.sh` |
| 8 | Automated tests (core, resiliency, trace, integration) | [§10 Tests & CI](#10-tests--ci) · `tests/`, `integration/` |
| 9 | README | this document |

**Bonuses:** Prometheus `/metrics` endpoint · OpenTelemetry + Jaeger trace
visualization · 100% line+branch coverage gate in CI.

---

## 12. Design notes & assumptions

- **`POST /events` is "call-first, no orphan rows"** — when the Account Service
  is down the Gateway returns `503` and stores **nothing**, keeping the invariant
  *a Gateway event exists ⟺ a matching Account transaction exists*. Retries are
  safe because the Account Service is the idempotency authority on `eventId` (a
  timed-out-but-applied call is deduped on retry, so the system converges with no
  double-apply and no lost transaction). Trade-offs: no audit of failed
  submissions, `GET /events` doesn't surface in-flight events during an outage,
  and the async-fallback bonus would need a separate outbox — none required by
  the brief; `status` is effectively always `APPLIED` (kept as headroom).
- **Money** — stored as `NUMERIC(18,2)`, returned as a rounded float for
  simplicity; production would use integer minor units or `Decimal` end-to-end.
- **Balance** assumes a single currency per account (the handout models balance
  as one number); mixed-currency would need per-currency balances.
- **Unknown account** balance/detail queries return `404` (an account "exists"
  once it has ≥1 transaction), so unknown is distinguishable from empty.
- **Balance proxy on the Gateway** — the handout lists balance only on the
  internal Account Service, but clients can only reach it through the Gateway, so
  the Gateway exposes a proxy that returns `503` when the backend is down.

---

## 13. Roadmap

**Done — all functional requirements + bonuses:** Gateway → Account apply call,
distributed tracing (header **and** OpenTelemetry → Jaeger), Prometheus custom
metric, graceful degradation, balance proxy, circuit breaker, and integration +
resiliency + trace tests.

**Remaining (optional bonuses):** an OTel Collector (if metrics/logs also move to
OTLP and need routing/fan-out), rate limiting on the Gateway, retry-with-backoff
for transient blips, and an async fallback (queue events locally when the Account
Service is down, replay on recovery).
