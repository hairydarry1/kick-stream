"""Discovery orchestrator — finds Kick streamers via the Kick API.

The only stage that could spend money, but Kick's public API is free.
Cost discipline is still built in: cache results, cap per run.
"""

import logging
from datetime import datetime, timezone

from kickstream.collectors.kick_api import KickAPIClient, discover_streamers
from kickstream.models.streamer import Streamer
from kickstream.signals import get_signals_for_collector
from kickstream.state import StateManager

logger = logging.getLogger("kickstream.discover")


async def run_discovery(
    state: StateManager,
    config,
    *,
    category: str | None = None,
    max_streamers: int = 100,
    min_followers: int = 500,
    max_followers: int = 50000,
    min_viewers: int = 0,
    max_viewers: int = 0,
    dry_run: bool = False,
) -> dict:
    """Run discovery via Kick API.

    Returns result dict with counts and any errors.
    """
    result = {
        "found": 0,
        "new_streamers": 0,
        "known_streamers": 0,
        "observations": 0,
        "errors": [],
        "stopped": "",
    }

    # Determine target category
    target_category = category or ""
    if not target_category and config and config.icp.categories:
        target_category = config.icp.categories[0]

    # Build a discovery run record
    run_id = None
    if not dry_run:
        from kickstream.state import StateManager as SM
        run_id = f"discover-{datetime.now(timezone.utc).isoformat()}"

    # Discover streamers
    client = KickAPIClient()
    try:
        streamers = await discover_streamers(
            client,
            category=target_category,
            max_streamers=max_streamers,
            min_followers=min_followers,
            max_followers=max_followers,
            min_viewers=min_viewers,
            max_viewers=max_viewers,
        )
    except Exception as e:
        result["errors"].append(f"Discovery failed: {e}")
        logger.error(f"Discovery failed: {e}")
        return result

    result["found"] = len(streamers)

    if dry_run:
        return result

    # Insert streamers into DB
    for streamer in streamers:
        existing = await state.get_streamer_by_kick_username(streamer.kick_username)
        if existing:
            result["known_streamers"] += 1
            # Update with fresh data
            await state.update_streamer(existing.id, **{
                "display_name": streamer.display_name,
                "followers": streamer.followers,
                "current_viewers": streamer.current_viewers,
                "total_hours_streamed": streamer.total_hours_streamed,
                "partner_status": streamer.partner_status,
                "category": streamer.category,
                "bio": streamer.bio,
            })
            continue

        streamer_id = await state.add_streamer(streamer)
        result["new_streamers"] += 1

        # Record observations for the confirmed signals
        signal_codes = get_signals_for_collector("kick_api")
        for code in signal_codes:
            obs = {
                "streamer_id": streamer_id,
                "signal_code": code,
                "collector": "kick_api",
                "confidence": 0.9,
                "run_id": run_id or "",
            }
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
            await state.add_observation(obs)
            result["observations"] += 1

    return result