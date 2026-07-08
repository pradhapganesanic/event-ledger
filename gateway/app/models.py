"""Event Gateway persistence model.

The Gateway stores the FULL event (all fields), because it must return the
original event on GET /events/{id} and on duplicate submissions, and those
reads must work even when the Account Service is down (Requirement #6).

`status` tracks the lifecycle of the downstream apply:
  PENDING  - stored locally, not yet applied to the account (Phase 1 default)
  APPLIED  - Account Service confirmed the transaction (Phase 2)
  FAILED   - Account Service was unreachable / rejected it (Phase 2)
"""
from datetime import datetime

from sqlalchemy import JSON, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class Event(Base):
    __tablename__ = "events"

    # eventId is the primary key => the database enforces idempotency.
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    account_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)  # CREDIT | DEBIT
    amount: Mapped[float] = mapped_column(Numeric(18, 2, asdecimal=False), nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    event_timestamp: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    # `metadata` is reserved on the Declarative Base, so map the attribute
    # `event_metadata` to a column literally named "metadata".
    event_metadata: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="PENDING")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    def to_dict(self) -> dict:
        return {
            "eventId": self.event_id,
            "accountId": self.account_id,
            "type": self.type,
            "amount": float(self.amount),
            "currency": self.currency,
            "eventTimestamp": self.event_timestamp.isoformat(),
            "metadata": self.event_metadata,
            "status": self.status,
        }
