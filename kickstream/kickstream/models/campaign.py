"""Campaign model — an email sequence sent to a group of contacts."""

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field


class EmailStep(BaseModel):
    step: int
    subject: str
    body: str
    delay_days: int = 0  # days after previous step (0 = immediate for step 1)


class Campaign(BaseModel):
    id: str = Field(default_factory=lambda: __import__('uuid').uuid4().hex[:12])
    name: str = ""
    channel: Literal["email"] = "email"
    sequence: list[EmailStep] = Field(default_factory=list)
    contact_ids: list[str] = Field(default_factory=list)
    status: Literal["draft", "active", "completed", "paused"] = "draft"
    created_at: datetime = Field(default_factory=datetime.utcnow)