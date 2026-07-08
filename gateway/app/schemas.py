"""Public request/response contracts for the Event Gateway."""
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


class EventIn(BaseModel):
    """Body of POST /events. Validation of required fields, amount > 0, and
    type in {CREDIT, DEBIT} is enforced here by Pydantic."""

    eventId: str = Field(min_length=1)
    accountId: str = Field(min_length=1)
    type: Literal["CREDIT", "DEBIT"]
    amount: float = Field(gt=0)
    currency: str = Field(min_length=1)
    eventTimestamp: datetime
    metadata: Optional[dict] = None
