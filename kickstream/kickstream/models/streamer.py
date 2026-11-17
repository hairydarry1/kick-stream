"""Streamer model — a Kick streamer (like a company in Sellbird)."""

from datetime import datetime
from pydantic import BaseModel, Field


class Streamer(BaseModel):
    id: str = Field(default_factory=lambda: __import__('uuid').uuid4().hex[:12])
    kick_username: str = ""
    display_name: str = ""
    profile_url: str = ""
    avatar_url: str = ""
    category: str = ""
    followers: int = 0
    current_viewers: int = 0  # From /livestreams endpoint
    total_hours_streamed: int = 0
    partner_status: str = ""  # partner, affiliate, none
    location: str = ""
    bio: str = ""
    external_id: str = ""  # kick:{username} for dedup
    domain: str = ""  # optional website
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Computed properties (not stored)
    tech_stack: list[str] = Field(default_factory=list, exclude=True)
    signals: list[dict] = Field(default_factory=list, exclude=True)

    @property
    def kick_url(self) -> str:
        return f"https://kick.com/{self.kick_username}"