"""Kick Public API client — discovers Kick streamers.

Kick's public API: https://api.kick.com/public/v1

Verified endpoints (as of Kick API documentation - https://docs.kick.com):
- GET /categories                — list categories with streamer counts
- GET /channels/{username}       — channel info (followers, category, etc.)
- GET /livestreams               — currently live streams (filterable by category)
- GET /channels/{username}/livestreams — past VODs

Rate limiting: Kick's public API doesn't document strict limits but we cache
aggressively and spread requests across heartbeat cycles.

Note: Kick's public API does NOT provide:
- Historical follower counts
- Average viewers over time (only total hours streamed)
- Search endpoint (undocumented)
"""

import asyncio
import json
import logging
import random
from datetime import datetime, timezone
from urllib.parse import quote_plus

import httpx

from kickstream.models.streamer import Streamer

logger = logging.getLogger("kickstream.kick_api")

API_BASE = "https://api.kick.com/public/v1"
WEB_BASE = "https://kick.com"

# HTTP hardening
HTTP_TIMEOUT = 15.0
MAX_RETRIES = 3
BACKOFF_BASE = 1.5

# Rate limiting
MIN_DELAY = 0.5

# User agents
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.1 Safari/605.1.15",
]


class KickAPIError(Exception):
    pass


class KickAPIClient:
    """Minimal async client for the Kick public API."""

    def __init__(self):
        self._last_request_at = 0.0

    async def _rate_limit(self):
        """Ensure minimum delay between requests."""
        now = asyncio.get_event_loop().time()
        elapsed = now - self._last_request_at
        if elapsed < MIN_DELAY:
            await asyncio.sleep(MIN_DELAY - elapsed + random.uniform(0, 0.3))
        self._last_request_at = asyncio.get_event_loop().time()

    async def _get(self, path: str, params: dict | None = None) -> dict:
        """GET with retries, backoff, and rate limiting."""
        await self._rate_limit()
        url = f"{API_BASE}{path}"

        for attempt in range(MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                    resp = await client.get(
                        url,
                        params=params,
                        headers={"User-Agent": random.choice(USER_AGENTS)},
                    )
            except (httpx.TimeoutException, httpx.TransportError) as e:
                logger.debug(f"Kick API transport error ({url[:80]}): {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(BACKOFF_BASE * (2 ** attempt))
                    continue
                return {}

            if resp.status_code == 200:
                try:
                    return resp.json() if resp.json() else {}
                except (json.JSONDecodeError, ValueError):
                    logger.debug(f"Kick API returned non-JSON for {url[:80]}")
                    return {}
            elif resp.status_code == 429:
                logger.debug(f"Kick API rate limited ({url[:80]}). Backing off...")
                await asyncio.sleep(BACKOFF_BASE * (2 ** attempt) * 2)
                continue
            elif resp.status_code in (500, 502, 503, 504):
                logger.debug(f"Kick API server error {resp.status_code} ({url[:80]})")
                await asyncio.sleep(BACKOFF_BASE * (2 ** attempt))
                continue
            else:
                logger.debug(f"Kick API {resp.status_code} for {url[:80]}")
                return {}
        return {}

    # ── Endpoints ──
    # Each endpoint's doc URL from Kick's official developer docs:
    # https://docs.kick.com

    async def get_categories(self) -> list[dict]:
        """List available categories.

        Docs: https://docs.kick.com/public-api/get-categories
        """
        data = await self._get("/categories")
        return data.get("data", []) if isinstance(data, dict) else []

    async def get_channel(self, username: str) -> dict:
        """Get channel info for a username.

        Docs: https://docs.kick.com/public-api/get-channel-information
        """
        data = await self._get(f"/channels/{quote_plus(username.lower())}")
        if isinstance(data, dict) and data.get("data"):
            return data["data"][0]
        return {}

    async def get_channels_by_category(self, category: str | None = None) -> list[dict]:
        """Get channels filtered by category.

        Docs: https://docs.kick.com/public-api/get-categories
        """
        all_channels = []
        offset = 0
        limit = 100

        while True:
            params = {"limit": limit, "offset": offset}
            if category:
                params["category"] = category
            data = await self._get("/channels", params=params)
            if not isinstance(data, dict) or "data" not in data:
                break
            batch = data.get("data", [])
            if not batch:
                break
            all_channels.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
            await asyncio.sleep(MIN_DELAY)  # Rate limiting

        return all_channels

    async def get_livestreams(self, category: str | None = None, language: str | None = None) -> list[dict]:
        """Get currently live streams, optionally filtered by category/language.

        Docs: https://docs.kick.com/public-api/get-livestreams
        """
        params = {}
        if category:
            params["category"] = category
        if language:
            params["language"] = language
        data = await self._get("/livestreams", params=params)
        if isinstance(data, dict) and data.get("data"):
            return data["data"]
        return []

    # ── Conversion helpers ──

    @staticmethod
    def channel_to_streamer(data: dict) -> Streamer | None:
        """Convert a Kick API channel dict to a Streamer model.

        Kick API verified fields from /channels endpoint:
        - user.username
        - user.profile_pic
        - user.location
        - user.bio
        - followers_count
        - total_hours_streamed
        - partner_status
        - category (nested)

        Kick API verified fields from /livestreams endpoint:
        - channel.user.username
        - channel.category.name
        - viewer_count (current live viewers)

        NOT available from public API:
        - historical average viewers (would require polling over time)
        """
        if not isinstance(data, dict):
            return None

        # Extract username from different possible locations
        username = (data.get("user") or {}).get("username") or data.get("username") or ""
        if not username:
            # Try channel.user.username for livestreams
            channel_user = (data.get("channel") or {}).get("user") or {}
            username = channel_user.get("username") or ""
            if not username:
                return None

        user = data.get("user") or {}
        channel_user = (data.get("channel") or {}).get("user") or {}
        category_obj = data.get("category") or (data.get("channel") or {}).get("category") or {}

        # Get current viewers from livestreams endpoint
        current_viewers = int(data.get("viewer_count", 0) or 0)

        return Streamer(
            kick_username=username.lower(),
            display_name=user.get("username") or channel_user.get("username") or username,
            profile_url=f"{WEB_BASE}/{username}",
            avatar_url=user.get("profile_pic") or channel_user.get("profile_pic") or "",
            category=category_obj.get("name") if isinstance(category_obj, dict) else "",
            followers=int(data.get("followers_count", 0) or 0),
            current_viewers=current_viewers,
            total_hours_streamed=int(data.get("total_hours_streamed", 0) or 0),
            partner_status=data.get("partner_status", "") or "",
            location=user.get("location") or channel_user.get("location") or "",
            bio=user.get("bio") or channel_user.get("bio") or "",
            external_id=f"kick:{username.lower()}",
        )

    @staticmethod
    def is_bot_challenge(text: str, status: int = 200) -> bool:
        """Detect Cloudflare/bot challenge pages."""
        challenge_indicators = [
            "just a moment", "checking your browser", "cf-challenge",
            "captcha", "enable javascript", "attention required",
            "verify you are human", "cf-browser-verification",
        ]
        lower = (text or "").lower()
        if any(ind in lower for ind in challenge_indicators):
            return True
        if status in (403, 429) and ("cloudflare" in lower or "challenge" in lower):
            return True
        return False


# ── High-level discovery functions ─────────────────────────────────────────


async def discover_streamers(
    client: KickAPIClient,
    *,
    category: str | None = None,
    max_streamers: int = 100,
    min_followers: int = 0,
    max_followers: int = 0,
    min_viewers: int = 0,
    max_viewers: int = 0,
) -> list[Streamer]:
    """Discover Kick streamers matching the criteria.

    Strategy: fetch live streams (optionally by category), then channels.
    Filters by follower counts and current viewer counts.
    """
    streamers: list[Streamer] = []
    seen_usernames: set[str] = set()

    # Strategy 1: live streams (optionally by category) - PRIMARY discovery path
    live_streams = await client.get_livestreams(category=category)
    for ls in live_streams:
        if len(streamers) >= max_streamers:
            break
        if not isinstance(ls, dict):
            continue
        streamer = client.channel_to_streamer(ls)
        if streamer and streamer.kick_username not in seen_usernames:
            if _passes_filters(streamer, min_followers, max_followers, min_viewers, max_viewers):
                streamers.append(streamer)
                seen_usernames.add(streamer.kick_username)

    # Strategy 2: if we don't have enough, fetch channels by category
    if len(streamers) < max_streamers and category:
        channels = await client.get_channels_by_category(category)
        for ch in channels:
            if len(streamers) >= max_streamers:
                break
            if not isinstance(ch, dict):
                continue
            streamer = client.channel_to_streamer(ch)
            if streamer and streamer.kick_username not in seen_usernames:
                if _passes_filters(streamer, min_followers, max_followers, min_viewers, max_viewers):
                    streamers.append(streamer)
                    seen_usernames.add(streamer.kick_username)

    return streamers


def _passes_filters(
    streamer: Streamer,
    min_followers: int,
    max_followers: int,
    min_viewers: int = 0,
    max_viewers: int = 0,
) -> bool:
    if min_followers and streamer.followers < min_followers:
        return False
    if max_followers and streamer.followers > max_followers:
        return False
    if min_viewers and streamer.current_viewers < min_viewers:
        return False
    if max_viewers and streamer.current_viewers > max_viewers:
        return False
    return True