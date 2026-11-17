"""Streamer profile scraper — collects buying signals from Kick profiles.

Scrapes the streamer's Kick profile page and linked pages (Linktree, etc.)
for:
- Social links (Twitter/X, Instagram, Discord, YouTube, TikTok)
- Business email in bio
- Donation links (StreamElements, Streamlabs, Ko-fi, Patreon)
- Merch links
- Sponsorship email / "business inquiries"
- OBS/overlay clues

No model calls, no cost. Python does all scraping.
"""

import asyncio
import logging
import random
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from kickstream.models.streamer import Streamer
from kickstream.state import StateManager

logger = logging.getLogger("kickstream.profile")

HTTP_TIMEOUT = 12.0
MAX_RETRIES = 2
BACKOFF_BASE = 1.5

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
URL_RE = re.compile(r"https?://[^\s<>\"']+")

# Domains indicating monetization intent
DONATION_DOMAINS = {
    "streamelements.com", "streamlabs.com", "kofi.com", "ko-fi.com",
    "patreon.com", "streamdonations.com", "paypal.com", "koji.com",
    "throne.com", "streamers.com",
}
MERCH_DOMAINS = {
    "fourthwall.com", "shopify.com", "streamlabs.com", "teespring.com",
    "spreadshirt.com", "redbubble.com", "printful.com", "printify.com",
}
SOCIAL_DOMAINS = {
    "twitter.com", "x.com", "instagram.com", "discord.com", "discord.gg",
    "youtube.com", "youtu.be", "tiktok.com", "twitch.tv", "kick.com",
    "linktr.ee", "beacons.ai", "koji.to", "allmylinks.com",
}


class ProfileScraper:
    """Scrapes a streamer's Kick profile for buying signals."""

    def __init__(self, state: StateManager | None = None):
        self.state = state

    def _headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }

    async def _fetch(self, url: str) -> str | None:
        """Fetch a URL with retries. Returns HTML text or None."""
        for attempt in range(MAX_RETRIES):
            try:
                async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
                    resp = await client.get(url, headers=self._headers())
                    if resp.status_code == 200:
                        # Check for bot challenges
                        text = resp.text
                        if any(ind in text.lower() for ind in (
                            "just a moment", "checking your browser",
                            "cf-challenge", "captcha",
                        )):
                            return None
                        return text
                    elif resp.status_code in (429, 500, 502, 503, 504):
                        if attempt < MAX_RETRIES - 1:
                            await asyncio.sleep(BACKOFF_BASE * (2 ** attempt))
                            continue
            except (httpx.TimeoutException, httpx.TransportError) as e:
                logger.debug(f"Profile fetch error ({url[:80]}): {e}")
            except Exception as e:
                logger.debug(f"Profile fetch unexpected ({url[:80]}): {e}")
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(BACKOFF_BASE * (2 ** attempt))
        return None

    @staticmethod
    def _extract_emails(text: str) -> list[str]:
        """Extract all emails from text, deduplicated."""
        emails = []
        for m in EMAIL_RE.finditer(text):
            email = m.group().lower().strip(".,;")
            # Filter obvious junk
            if email.endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")):
                continue
            if email.endswith((".js", ".css", ".woff", ".woff2")):
                continue
            if email not in emails:
                emails.append(email)
        return emails

    @staticmethod
    def _extract_urls(text: str) -> list[str]:
        """Extract all URLs from text, deduplicated."""
        urls = []
        for m in URL_RE.finditer(text):
            url = m.group().strip(".,;\"'")
            if url and url not in urls:
                urls.append(url)
        return urls

    async def scrape_profile(self, streamer: Streamer) -> dict:
        """Scrape the streamer's Kick profile. Returns observations dict."""
        profile_url = streamer.profile_url or f"https://kick.com/{streamer.kick_username}"
        html = await self._fetch(profile_url)
        if not html:
            return {"error": "profile_unavailable"}

        observations = {}
        soup = BeautifulSoup(html, "html.parser")

        # Extract text content
        text = soup.get_text(" ", strip=True)
        raw_text = html

        # Extract all URLs from the page
        urls = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.startswith("/"):
                href = urljoin(profile_url, href)
            if href and href not in urls:
                urls.append(href)
        for m in URL_RE.finditer(raw_text):
            url = m.group().strip(".,;\"'")
            if url and url not in urls:
                urls.append(url)

        # ── Signal: Business email ──
        emails = self._extract_emails(text)
        if emails:
            observations["HAS_BUSINESS_EMAIL"] = True
            observations["_emails"] = emails

        # ── Signal: Donation link ──
        donation_urls = [u for u in urls if any(d in u.lower() for d in DONATION_DOMAINS)]
        if donation_urls:
            observations["HAS_DONATION_LINK"] = True
            observations["_donation_urls"] = donation_urls[:3]

        # ── Signal: Merch link ──
        merch_urls = [u for u in urls if any(d in u.lower() for d in MERCH_DOMAINS)]
        if merch_urls:
            observations["HAS_MERCH_LINK"] = True
            observations["_merch_urls"] = merch_urls[:3]

        # ── Signal: Sponsorship email / business inquiries ──
        lower_text = text.lower()
        sponsorship_keywords = [
            "business inquiries", "business inquiry", "sponsorship", "sponsor",
            "collab", "partnership", "management", "agent", "book me",
        ]
        if any(kw in lower_text for kw in sponsorship_keywords):
            observations["HAS_SPONSORSHIP_EMAIL"] = True

        # ── Signal: StreamElements / Streamlabs usage ──
        if "streamelements" in lower_text or "streamelements" in raw_text.lower():
            observations["USES_STREAMELEMENTS"] = True
        if "streamlabs" in lower_text or "streamlabs" in raw_text.lower():
            observations["USES_STREAMLABS"] = True

        # ── Signal: Custom overlay ──
        # Look for custom CSS/JS references or unique widget configs
        overlay_keywords = [
            "custom overlay", "custom alerts", "overlay", "alerts",
            "widget", "scene", "transition",
        ]
        if any(kw in lower_text for kw in overlay_keywords):
            observations["HAS_CUSTOM_OVERLAY"] = True

        # ── Signal: Recent brand deal ──
        brand_keywords = [
            "sponsored by", "sponsor", "brought to you by",
            "thanks to our sponsor", "in partnership with",
        ]
        if any(kw in lower_text for kw in brand_keywords):
            observations["RECENT_BRAND_DEAL"] = True

        # ── Signal: Hiring editor/mod ──
        hiring_keywords = [
            "looking for editor", "hiring editor", "need editor",
            "looking for mod", "hiring mods", "need mods",
            "looking for video editor", "hiring video editor",
            "looking for a manager", "hiring manager",
            "looking for team", "hiring team",
        ]
        if any(kw in lower_text for kw in hiring_keywords):
            observations["HIRING_EDITOR_MOD"] = True

        # ── Signal: Twitter/X DMs open ──
        twitter_urls = [u for u in urls if "twitter.com" in u.lower() or "x.com" in u.lower()]
        if twitter_urls:
            observations["TWITTER_DM_OPEN"] = True
            observations["_twitter_urls"] = twitter_urls[:2]

        # ── Signal: Contact form URL ──
        contact_urls = [u for u in urls if any(c in u.lower() for c in ("/contact", "/contact-us"))]
        if contact_urls:
            observations["CONTACT_FORM_URL"] = contact_urls[0]

        # ── Extract social links for later use ──
        social_links = {}
        for u in urls:
            host = urlparse(u).netloc.lower().replace("www.", "")
            for domain in SOCIAL_DOMAINS:
                if domain in host:
                    social_links[domain.split(".")[0]] = u
                    break
        if social_links:
            observations["_social_links"] = social_links

        return observations


async def run_profile_stage(
    state: StateManager,
    limit: int = 50,
    stale_days: int = 30,
    config=None,
) -> tuple[int, int, list[dict]]:
    """Scrape streamer profiles for buying signals.

    Returns (profiles_scraped, observations_written, errors).
    """
    scraper = ProfileScraper(state)
    streamers = await state.list_streamers(limit=limit)
    observations_written = 0
    errors = []
    scraped = 0

    for streamer in streamers:
        try:
            result = await scraper.scrape_profile(streamer)
            if "error" in result:
                errors.append(f"{streamer.kick_username}: {result['error']}")
                continue

            scraped += 1

            # Write observations
            signal_map = {
                "HAS_BUSINESS_EMAIL": "boolean",
                "HAS_DONATION_LINK": "boolean",
                "HAS_MERCH_LINK": "boolean",
                "HAS_SPONSORSHIP_EMAIL": "boolean",
                "USES_STREAMELEMENTS": "boolean",
                "USES_STREAMLABS": "boolean",
                "HAS_CUSTOM_OVERLAY": "boolean",
                "RECENT_BRAND_DEAL": "boolean",
                "HIRING_EDITOR_MOD": "boolean",
                "TWITTER_DM_OPEN": "boolean",
                "CONTACT_FORM_URL": "text",
            }

            for signal_code, value_type in signal_map.items():
                if signal_code in result:
                    value = result[signal_code]
                    if value_type == "boolean":
                        obs = {
                            "streamer_id": streamer.id,
                            "signal_code": signal_code,
                            "collector": "profile_scraper",
                            "value_num": 1 if value else 0,
                            "value_text": "true" if value else "false",
                            "confidence": 0.8,
                        }
                    else:
                        obs = {
                            "streamer_id": streamer.id,
                            "signal_code": signal_code,
                            "collector": "profile_scraper",
                            "value_text": str(value),
                            "confidence": 0.8,
                        }
                    await state.add_observation(obs)
                    observations_written += 1

            # Update streamer with profile data
            updates = {}
            if result.get("_social_links"):
                updates["bio"] = streamer.bio
            if updates:
                await state.update_streamer(streamer.id, **updates)

            # Mark as profiled
            await state.update_streamer(streamer.id, profile_checked_at=datetime.now(timezone.utc).isoformat())

        except Exception as e:
            errors.append(f"{streamer.kick_username}: {e}")
            logger.warning(f"Profile scrape failed for {streamer.kick_username}: {e}")

    return scraped, observations_written, errors