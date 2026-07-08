"""Request/response contracts for the Account Service.

This is the internal API contract the Gateway codes against (Requirement #2:
"Define clear API contracts between the services").
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class TransactionIn(BaseModel):
    """Body of POST /accounts/{accountId}/transactions.

    accountId comes from the path. Amount must be > 0 and type constrained to
    CREDIT/DEBIT — validation is enforced here by Pydantic.
    """

    eventId: str = Field(min_length=1)
    type: Literal["CREDIT", "DEBIT"]
    amount: float = Field(gt=0)
    currency: str = Field(min_length=1)
    transactionTimestamp: datetime
