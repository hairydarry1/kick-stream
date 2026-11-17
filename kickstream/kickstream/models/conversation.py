"""Conversation model — an email thread with a contact."""

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field


class Message(BaseModel):
    id: str = Field(default_factory=lambda: __import__('uuid').uuid4().hex[:12])
    role: Literal["user", "assistant"]  # user = from contact, assistant = from us
    body: str
    sent_at: datetime = Field(default_factory=datetime.utcnow)
    message_id: str = ""  # email Message-ID header
    in_reply_to: str = ""


class Conversation(BaseModel):
    id: str = Field(default_factory=lambda: __import__('uuid').uuid4().hex[:12])
    contact_id: str = ""
    campaign_id: str = ""
    channel: Literal["email"] = "email"
    thread: list[Message] = Field(default_factory=list)
    intent: str = ""  # interested, objection, question, wrong_person, unsubscribe, ooo, escalate
    stage: Literal[
        "initial_outreach", "engaged", "qualifying", "presenting", "negotiating", "closing", "closed_won", "closed_lost"
    ] = "initial_outreach"
    status: Literal["open", "closed", "closed_won", "closed_lost", "objection", "question", "ooo", "wrong_person", "unsubscribe", "escalate"] = "open"
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def last_message(self) -> Message | None:
        return self.thread[-1] if self.thread else None

    @property
    def is_open(self) -> bool:
        return self.status == "open"