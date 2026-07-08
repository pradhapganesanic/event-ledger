"""Account Service persistence model.

The Account Service is the AUTHORITATIVE owner of account state: it stores the
transactions that were actually applied and computes balance from them.

Note the domain rename: the Gateway calls this an `eventTimestamp`; inside the
account domain it is a `transaction_timestamp`.
"""
from datetime import datetime

from sqlalchemy import DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # `event_id` carries the Gateway's eventId so the Account Service can be
    # idempotent against Gateway retries (Phase 2). Unique => at-most-once apply.
    event_id: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    account_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)  # CREDIT | DEBIT
    amount: Mapped[float] = mapped_column(Numeric(18, 2, asdecimal=False), nullable=False)
    currency: Mapped[str] = mapped_column(String, nullable=False)
    transaction_timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    def to_dict(self) -> dict:
        return {
            "eventId": self.event_id,
            "accountId": self.account_id,
            "type": self.type,
            "amount": float(self.amount),
            "currency": self.currency,
            "transactionTimestamp": self.transaction_timestamp.isoformat(),
        }
