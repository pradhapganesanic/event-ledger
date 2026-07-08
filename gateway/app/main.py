"""Event Gateway (public-facing) — the entry point for all client requests.

Endpoints:
  POST /events                 submit a transaction event (validate + dedup + store)
  GET  /events/{id}            retrieve a single event (local data only)
  GET  /events?account={id}    list an account's events, ordered by eventTimestamp
  GET  /health                 status + DB connectivity
  GET  /metrics                per-endpoint counters

PHASE 1 SCOPE: this stores events in the Gateway's own DB and enforces
idempotency. The call to the Account Service to *apply* the transaction, plus
trace propagation, resiliency, and graceful degradation, are Phase 2 (see the
TODO in create_event). GET reads already depend only on local data.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import metrics
from .database import get_db, init_db
from .logging_config import SERVICE_NAME, configure_logging
from .models import Event
from .schemas import EventIn

configure_logging()
log = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info("event-gateway starting", extra={"extra_fields": {"event": "startup"}})
    yield


app = FastAPI(title="Event Gateway", lifespan=lifespan)
metrics.install(app, SERVICE_NAME)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    """Return 400 with clear, serialisable messages for invalid input."""
    details = [
        {"field": ".".join(str(p) for p in e["loc"] if p != "body"), "message": e["msg"]}
        for e in exc.errors()
    ]
    return JSONResponse(
        status_code=400,
        content={"error": "validation_error", "message": "Invalid request payload", "details": details},
    )


@app.post("/events", status_code=201)
def create_event(body: EventIn, response: Response, db: Session = Depends(get_db)):
    """Validate, enforce idempotency, and store the event locally.

    Idempotency: a repeated eventId returns the original event with 200 and does
    not create a duplicate.
    """
    existing = db.get(Event, body.eventId)
    if existing is not None:
        response.status_code = 200
        log.info("duplicate event ignored", extra={"extra_fields": {"eventId": body.eventId}})
        return existing.to_dict()

    event = Event(
        event_id=body.eventId,
        account_id=body.accountId,
        type=body.type,
        amount=body.amount,
        currency=body.currency,
        event_timestamp=body.eventTimestamp,
        event_metadata=body.metadata,
        status="PENDING",
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
        extra={"extra_fields": {"eventId": event.event_id, "accountId": event.account_id}},
    )

    # TODO(Phase 2): switch to the locked "call-first, no orphan rows" contract
    # (see README "Phase 2 design decision"): call Account Service
    # POST /accounts/{id}/transactions BEFORE persisting, with trace propagation
    # + resiliency. On success store the event (APPLIED) and return 201; if the
    # Account Service is down/breaker-open, return 503 and store NOTHING. The
    # Account Service's idempotency on eventId makes retries after a 503 safe.
    return event.to_dict()


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
