"""Event Gateway (public-facing) — the entry point for all client requests.

Endpoints:
  POST /events                    submit a transaction event (validate + apply + store)
  GET  /events/{id}               retrieve a single event (local data only)
  GET  /events?account={id}       list an account's events, ordered by eventTimestamp
  GET  /accounts/{id}/balance     balance proxy to the Account Service
  GET  /health                    status + DB connectivity

POST /events uses the "call-first, no orphan rows" contract: it calls the
Account Service to apply the transaction BEFORE persisting locally. On success
the event is stored (APPLIED) and 201 returned; if the Account Service is
unavailable the Gateway returns 503 and stores nothing. GET reads depend only on
local data and keep working during an Account Service outage.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import account_client
from .account_client import AccountServiceUnavailable
from .database import get_db, init_db
from .logging_config import SERVICE_NAME, configure_logging
from .models import Event
from .schemas import EventIn
from .tracing import TRACE_HEADER, new_trace_id, set_trace_id

configure_logging()
log = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info("event-gateway starting", extra={"extra_fields": {"event": "startup"}})
    yield


app = FastAPI(title="Event Gateway", lifespan=lifespan)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """Origin of the trace: generate a trace ID per request (or honour one an
    upstream caller already supplied), expose it to logging via the contextvar,
    and echo it on the response. It is propagated downstream by account_client."""
    trace_id = request.headers.get(TRACE_HEADER) or new_trace_id()
    set_trace_id(trace_id)
    response = await call_next(request)
    response.headers[TRACE_HEADER] = trace_id
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    """Return 400 with clear, serialisable messages for invalid input."""
    details = [
        {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
        for e in exc.errors()
    ]
    log.info(
        "event rejected",
        extra={"extra_fields": {"outcome": "rejected", "reason": "validation_error", "details": details}},
    )
    return JSONResponse(
        status_code=400,
        content={"error": "validation_error", "message": "Invalid request payload", "details": details},
    )


@app.post("/events", status_code=201)
def create_event(body: EventIn, response: Response, db: Session = Depends(get_db)):
    """Validate, enforce idempotency, apply to the Account Service, then store.

    Contract: "call-first, no orphan rows".
      - duplicate eventId               -> return original, 200 (idempotent)
      - Account Service applies it       -> store event (APPLIED), 201
      - Account Service unavailable      -> 503, store NOTHING

    A Gateway event therefore exists iff a matching Account transaction exists.
    Retries after a 503 are safe because the Account Service is idempotent on
    eventId.
    """
    existing = db.get(Event, body.eventId)
    if existing is not None:
        response.status_code = 200
        log.info(
            "event duplicate ignored",
            extra={"extra_fields": {"outcome": "duplicate", "eventId": body.eventId, "accountId": body.accountId}},
        )
        return existing.to_dict()

    # Call the Account Service FIRST. Its domain rename: eventTimestamp -> transactionTimestamp.
    payload = {
        "eventId": body.eventId,
        "type": body.type,
        "amount": body.amount,
        "currency": body.currency,
        "transactionTimestamp": body.eventTimestamp.isoformat(),
    }
    try:
        account_client.apply_transaction(body.accountId, payload)
    except AccountServiceUnavailable as exc:
        log.warning(
            "event apply failed",
            extra={"extra_fields": {"outcome": "failed", "eventId": body.eventId, "accountId": body.accountId, "reason": str(exc)}},
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": "account_service_unavailable",
                "message": "Account Service is unreachable; event was not applied. Please retry.",
            },
        )

    # Applied downstream -> persist locally as APPLIED.
    event = Event(
        event_id=body.eventId,
        account_id=body.accountId,
        type=body.type,
        amount=body.amount,
        currency=body.currency,
        event_timestamp=body.eventTimestamp,
        event_metadata=body.metadata,
        status="APPLIED",
    )
    db.add(event)
    try:
        db.commit()
    except IntegrityError:
        # Concurrent duplicate slipped past the read above.
        db.rollback()
        response.status_code = 200
        return db.get(Event, body.eventId).to_dict()

    db.refresh(event)
    log.info(
        "event stored",
        extra={"extra_fields": {"outcome": "stored", "eventId": event.event_id, "accountId": event.account_id}},
    )
    return event.to_dict()


@app.get("/accounts/{account_id}/balance")
def get_balance_proxy(account_id: str):
    """Proxy balance queries to the Account Service (which owns balance).

    The Account Service is internal-only, so clients reach balance through the
    Gateway. Returns a clear 503 when the Account Service is unreachable
    (graceful degradation) and 404 for an unknown account.
    """
    try:
        result = account_client.get_balance(account_id)
    except AccountServiceUnavailable as exc:
        log.warning(
            "balance query failed",
            extra={"extra_fields": {"outcome": "failed", "accountId": account_id, "reason": str(exc)}},
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": "account_service_unavailable",
                "message": "Balance is temporarily unavailable; the Account Service is unreachable.",
            },
        )
    if result is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    return result


@app.get("/events/{event_id}")
def get_event(event_id: str, db: Session = Depends(get_db)):
    """Retrieve one event from local data (works even if Account Service down)."""
    event = db.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail=f"Event {event_id} not found")
    return event.to_dict()


@app.get("/events")
def list_events(account: str = Query(...), db: Session = Depends(get_db)):
    """List an account's events ordered chronologically by eventTimestamp.

    Ordering is by event timestamp, NOT arrival order, so out-of-order delivery
    still produces a chronological listing (Requirement #1)."""
    events = db.scalars(
        select(Event)
        .where(Event.account_id == account)
        .order_by(Event.event_timestamp.asc())
    ).all()
    return {"account": account, "events": [e.to_dict() for e in events]}


@app.get("/health")
def health(response: Response, db: Session = Depends(get_db)):
    try:
        db.execute(select(1))
        db_ok = True
    except Exception:  # pragma: no cover - defensive
        db_ok = False
    if not db_ok:
        response.status_code = 503
    return {
        "status": "ok" if db_ok else "degraded",
        "service": SERVICE_NAME,
        "database": "connected" if db_ok else "disconnected",
    }
