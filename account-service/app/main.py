"""Account Service (internal) — owns account state and balance.

Endpoints:
  POST /accounts/{accountId}/transactions  apply a transaction (idempotent)
  GET  /accounts/{accountId}/balance       current balance
  GET  /accounts/{accountId}               details + recent transactions
  GET  /health                             status + DB connectivity
  GET  /metrics                            Prometheus metrics

Balance = sum(CREDIT) - sum(DEBIT), computed from applied transactions, so it
is correct regardless of the order in which transactions arrive.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import metrics, otel
from .database import get_db, init_db
from .logging_config import SERVICE_NAME, configure_logging
from .models import Transaction
from .schemas import TransactionIn
from .tracing import TRACE_HEADER, new_trace_id, set_trace_id

configure_logging()
log = logging.getLogger(SERVICE_NAME)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    log.info("account-service starting", extra={"extra_fields": {"event": "startup"}})
    yield


app = FastAPI(title="Account Service", lifespan=lifespan)
otel.setup_tracing(app)
metrics.install(app)


@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """Read the propagated trace ID (or generate one), expose it to logging via
    the contextvar, and echo it on the response."""
    trace_id = request.headers.get(TRACE_HEADER) or new_trace_id()
    set_trace_id(trace_id)
    response = await call_next(request)
    response.headers[TRACE_HEADER] = trace_id
    return response


def _balance_for(db: Session, account_id: str) -> float:
    credits = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.account_id == account_id, Transaction.type == "CREDIT"
        )
    )
    debits = db.scalar(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(
            Transaction.account_id == account_id, Transaction.type == "DEBIT"
        )
    )
    return round(float(credits) - float(debits), 2)


def _account_exists(db: Session, account_id: str) -> bool:
    return db.scalar(
        select(Transaction.id).where(Transaction.account_id == account_id).limit(1)
    ) is not None


@app.post("/accounts/{account_id}/transactions", status_code=201)
def apply_transaction(
    account_id: str,
    body: TransactionIn,
    response: Response,
    db: Session = Depends(get_db),
):
    """Apply a transaction. Idempotent on eventId: a duplicate returns the
    already-applied transaction with 200 and does NOT alter the balance."""
    # Fast path: recognise a duplicate cheaply.
    existing = db.scalar(select(Transaction).where(Transaction.event_id == body.eventId))
    if existing is not None:
        response.status_code = 200
        metrics.TRANSACTIONS.labels("duplicate").inc()
        log.info(
            "transaction duplicate ignored",
            extra={"extra_fields": {"outcome": "duplicate", "eventId": body.eventId, "accountId": account_id}},
        )
        return existing.to_dict()

    txn = Transaction(
        event_id=body.eventId,
        account_id=account_id,
        type=body.type,
        amount=body.amount,
        currency=body.currency,
        transaction_timestamp=body.transactionTimestamp,
    )
    db.add(txn)
    try:
        db.commit()
    except IntegrityError:
        # Safety net: concurrent duplicate slipped past the fast-path check.
        db.rollback()
        existing = db.scalar(select(Transaction).where(Transaction.event_id == body.eventId))
        response.status_code = 200
        return existing.to_dict()

    db.refresh(txn)
    metrics.TRANSACTIONS.labels("applied").inc()
    log.info(
        "transaction applied",
        extra={"extra_fields": {"outcome": "applied", "eventId": txn.event_id, "accountId": account_id, "type": txn.type, "amount": float(txn.amount)}},
    )
    return txn.to_dict()


@app.get("/accounts/{account_id}/balance")
def get_balance(account_id: str, db: Session = Depends(get_db)):
    if not _account_exists(db, account_id):
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    return {"accountId": account_id, "balance": _balance_for(db, account_id)}


@app.get("/accounts/{account_id}")
def get_account(account_id: str, db: Session = Depends(get_db)):
    if not _account_exists(db, account_id):
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    recent = db.scalars(
        select(Transaction)
        .where(Transaction.account_id == account_id)
        .order_by(Transaction.transaction_timestamp.desc())
        .limit(10)
    ).all()
    return {
        "accountId": account_id,
        "balance": _balance_for(db, account_id),
        "recentTransactions": [t.to_dict() for t in recent],
    }


@app.get("/health")
def health(response: Response, db: Session = Depends(get_db)):
    try:
        db.execute(select(1))
        db_ok = True
    except Exception:
        db_ok = False
    if not db_ok:
        response.status_code = 503
    return {
        "status": "ok" if db_ok else "degraded",
        "service": SERVICE_NAME,
        "database": "connected" if db_ok else "disconnected",
    }
