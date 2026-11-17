"""Scout — finds Kick streamers and their buying signals.

Python does ALL web scraping and API calls. Claude only analyzes, scores,
and personalizes data that Python already found.
"""

import asyncio
import logging
from datetime import datetime, timezone

from kickstream.brain import Brain
from kickstream.collectors.kick_api import KickAPIClient, discover_streamers
from kickstream.collectors.profile import ProfileScraper
from kickstream.config import KickstreamConfig, EnvConfig
from kickstream.models.streamer import Streamer
from kickstream.signals import get_signals_for_collector
from kickstream.state import StateManager

logger = logging.getLogger("kickstream.scout")

# Max streamers per cycle
MAX_STREAMERS_PER_CYCLE = 10


class Scout:
    def __init__(
        self,
        brain: Brain,
        state: StateManager,
        config: KickstreamConfig,
        env: EnvConfig,
    ):
        self.brain = brain
        self.state = state
        self.config = config
        self.env = env
        self.skills = ""

    async def run(self):
        """Main prospecting flow: find streamers matching ICP."""
        logger.info("Scout: Starting prospecting cycle...")

        self.skills = self.brain.load_skills_for_agent("scout")

        streamers_found = 0

        # Strategy 1: Kick API discovery (by ICP categories)
        try:
            streamers_found += await self._discover_via_kick_api()
        except Exception as e:
            logger.warning(f"Scout: kick_api strategy failed: {e}")

        # Strategy 2: Profile scraping (buying signals)
        try:
            streamers_found += await self._scrape_profiles()
        except Exception as e:
            logger.warning(f"Scout: profile strategy failed: {e}")

        await self.state.log_action(
            action_type="prospect",
            agent="scout",
            details={"streamers_found": streamers_found},
        )
        logger.info(f"Scout: Found {streamers_found} new streamers this cycle.")

    async def _discover_via_kick_api(self) -> int:
        """Discover streamers via the Kick API."""
        logger.info("Scout: Discovering streamers via Kick API...")

        icp = self.config.icp
        client = KickAPIClient()
        found = 0

        for category in icp.categories[:2]:  # Max 2 categories per cycle
            try:
                streamers = await discover_streamers(
                    client,
                    category=category,
                    max_streamers=MAX_STREAMERS_PER_CYCLE,
                    min_followers=icp.min_followers,
                    max_followers=icp.max_followers,
                    min_viewers=icp.min_viewers,
                    max_viewers=icp.max_viewers,
                )
            except Exception as e:
                logger.warning(f"Scout: Kick API discovery failed for {category}: {e}")
                continue

            for streamer in streamers:
                existing = await self.state.get_streamer_by_kick_username(streamer.kick_username)
                if existing:
                    continue

                streamer_id = await self.state.add_streamer(streamer)
                found += 1
                logger.info(f"Scout: Found new streamer @{streamer.kick_username} ({streamer.followers} followers)")

                # Record observations for confirmed signals
                for code in get_signals_for_collector("kick_api"):
                    obs = {"streamer_id": streamer_id, "signal_code": code, "collector": "kick_api"}
                    if code == "KICK_FOLLOWERS":
                        obs["value_num"] = streamer.followers
                    elif code == "KICK_CURRENT_VIEWERS":
                        obs["value_num"] = streamer.current_viewers
                    elif code == "KICK_CATEGORY":
                        obs["value_text"] = streamer.category
                    elif code == "KICK_PARTNER_STATUS":
                        obs["value_text"] = streamer.partner_status
                    elif code == "KICK_HOURS_STREAMED":
                        obs["value_num"] = streamer.total_hours_streamed
                    else:
                        continue
                    await self.state.add_observation(obs)

        return found

    async def _scrape_profiles(self) -> int:
        """Scrape streamer profiles for buying signals (free)."""
        logger.info("Scout: Scraping streamer profiles...")

        scraper = ProfileScraper(self.state)
        # Get streamers that haven't been profiled yet
        streamers = await self.state.list_streamers(limit=MAX_STREAMERS_PER_CYCLE)
        count = 0

        for streamer in streamers:
            try:
                result = await scraper.scrape_profile(streamer)
                if "error" in result:
                    continue

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
                        await self.state.add_observation(obs)

                await self.state.update_streamer(
                    streamer.id,
                    profile_checked_at=datetime.now(timezone.utc).isoformat(),
                )
                count += 1
            except Exception as e:
                logger.warning(f"Scout: profile scrape failed for {streamer.kick_username}: {e}")

        return count