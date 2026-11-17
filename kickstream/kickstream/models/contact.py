"""Contact model — a person at a streamer (like a prospect in Sellbird)."""

from datetime import datetime
from pydantic import BaseModel, Field


class Contact(BaseModel):
    id: str = Field(default_factory=lambda: __import__('uuid').uuid4().hex[:12])
    streamer_id: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    email_status: str = "guess"  # verified, risky, guess, invalid
    email_source: str = ""  # bio, linktree, twitter, contact_form, guessed
    twitter_handle: str = ""
    discord: str = ""
    contact_form_url: str = ""
    role: str = ""  # streamer, manager, business
    status: str = "new"  # new, contacted, replied, interested, meeting, not_interested, bounced, write_failed, opted_out
    score: int = 0
    personalization_notes: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def is_deliverable(self) -> bool:
        return self.email_status in ("verified", "risky")