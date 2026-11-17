"""Kickstream signal catalog — the governed vocabulary of streamer facts.

Sellbird PROPOSES signals, the user CONFIRMS which ones to collect.
Only confirmed signals get collected.
"""

from kickstream.state import StateManager


# ── Signal Catalog Definition ──────────────────────────────────────────────

KICK_SIGNALS = [
    # ── Discovery (free via Kick API) ──────────────────────────────────────
    {
        "code": "KICK_FOLLOWERS",
        "label": "Follower count",
        "description": "Total followers on Kick. Indicates audience size.",
        "category": "discovery",
        "value_type": "number",
        "collector": "kick_api",
        "cost_note": "free",
        "confidence_floor": 0.9,
    },
    {
        "code": "KICK_CURRENT_VIEWERS",
        "label": "Current live viewers",
        "description": "Current live viewer count from /livestreams endpoint. Strong indicator of engagement.",
        "category": "discovery",
        "value_type": "number",
        "collector": "kick_api",
        "cost_note": "free",
        "confidence_floor": 0.9,
    },
    {
        "code": "KICK_CATEGORY",
        "label": "Primary category",
        "description": "Main streaming category (e.g., Just Chatting, Gaming, Music). Determines content type.",
        "category": "discovery",
        "value_type": "text",
        "collector": "kick_api",
        "cost_note": "free",
        "confidence_floor": 1.0,
    },
    {
        "code": "KICK_PARTNER_STATUS",
        "label": "Partner/Affiliate status",
        "description": "Whether the streamer is a Kick partner, affiliate, or neither. Partners have monetization features.",
        "category": "discovery",
        "value_type": "text",
        "collector": "kick_api",
        "cost_note": "free",
        "confidence_floor": 1.0,
    },
    {
        "code": "KICK_HOURS_STREAMED",
        "label": "Total hours streamed",
        "description": "Cumulative streaming hours. Indicates commitment and consistency.",
        "category": "discovery",
        "value_type": "number",
        "collector": "kick_api",
        "cost_note": "free",
        "confidence_floor": 0.7,
    },
    {
        "code": "KICK_STREAM_FREQUENCY",
        "label": "Stream frequency (days/week)",
        "description": "How many days per week they stream on average. Consistency = professional approach.",
        "category": "discovery",
        "value_type": "number",
        "collector": "kick_api",
        "cost_note": "free",
        "confidence_floor": 0.6,
    },

    # ── Profile / Buying Intent (free scrape) ──────────────────────────────
    {
        "code": "HAS_BUSINESS_EMAIL",
        "label": "Business email in bio",
        "description": "Streamer lists a business/contact email in their Kick bio. Strong buying signal.",
        "category": "profile",
        "value_type": "boolean",
        "collector": "profile_scraper",
        "cost_note": "free",
        "confidence_floor": 0.9,
    },
    {
        "code": "HAS_DONATION_LINK",
        "label": "Donation/tip link",
        "description": "Has StreamElements, Streamlabs, Ko-fi, Patreon, or similar. Shows monetization mindset.",
        "category": "profile",
        "value_type": "boolean",
        "collector": "profile_scraper",
        "cost_note": "free",
        "confidence_floor": 0.8,
    },
    {
        "code": "HAS_MERCH_LINK",
        "label": "Merch store link",
        "description": "Links to merch (StreamElements, Fourthwall, Shopify, etc.). Already selling to audience.",
        "category": "profile",
        "value_type": "boolean",
        "collector": "profile_scraper",
        "cost_note": "free",
        "confidence_floor": 0.8,
    },
    {
        "code": "HAS_SPONSORSHIP_EMAIL",
        "label": "Sponsorship/business email",
        "description": "Explicitly lists an email for sponsorships, business inquiries, or collabs.",
        "category": "profile",
        "value_type": "boolean",
        "collector": "profile_scraper",
        "cost_note": "free",
        "confidence_floor": 0.9,
    },
    {
        "code": "USES_STREAMELEMENTS",
        "label": "Uses StreamElements",
        "description": "StreamElements widgets/overlays detected. May need custom overlays/alerts.",
        "category": "profile",
        "value_type": "boolean",
        "collector": "profile_scraper",
        "cost_note": "free",
        "confidence_floor": 0.7,
    },
    {
        "code": "USES_STREAMLABS",
        "label": "Uses Streamlabs",
        "description": "Streamlabs widgets/overlays detected. May need custom overlays/alerts.",
        "category": "profile",
        "value_type": "boolean",
        "collector": "profile_scraper",
        "cost_note": "free",
        "confidence_floor": 0.7,
    },
    {
        "code": "HAS_CUSTOM_OVERLAY",
        "label": "Custom overlay detected",
        "description": "Visual evidence of custom (non-template) overlay. Shows investment in brand.",
        "category": "profile",
        "value_type": "boolean",
        "collector": "profile_scraper",
        "cost_note": "free",
        "confidence_floor": 0.6,
    },
    {
        "code": "RECENT_BRAND_DEAL",
        "label": "Recent brand deal mentioned",
        "description": "Bio or recent stream mentions a sponsor/brand deal. Active in creator economy.",
        "category": "profile",
        "value_type": "boolean",
        "collector": "profile_scraper",
        "cost_note": "free",
        "confidence_floor": 0.5,
    },
    {
        "code": "HIRING_EDITOR_MOD",
        "label": "Hiring editor/mods",
        "description": "Bio or social posts mention looking for video editor, moderator, or channel manager.",
        "category": "profile",
        "value_type": "boolean",
        "collector": "profile_scraper",
        "cost_note": "free",
        "confidence_floor": 0.8,
    },

    # ── People / Contact ───────────────────────────────────────────────────
    {
        "code": "STREAMER_EMAIL",
        "label": "Streamer email found",
        "description": "Email address found for the streamer (bio, Linktree, Twitter, etc.).",
        "category": "people",
        "value_type": "boolean",
        "collector": "streamer_contact_finder",
        "cost_note": "free",
        "confidence_floor": 0.7,
    },
    {
        "code": "MANAGER_CONTACT",
        "label": "Manager/agency contact found",
        "description": "Contact info for a manager, agent, or agency representing the streamer.",
        "category": "people",
        "value_type": "boolean",
        "collector": "streamer_contact_finder",
        "cost_note": "free",
        "confidence_floor": 0.6,
    },
    {
        "code": "TWITTER_DM_OPEN",
        "label": "Twitter/X DMs open",
        "description": "Streamer has open DMs on Twitter/X. Alternative outreach channel.",
        "category": "people",
        "value_type": "boolean",
        "collector": "profile_scraper",
        "cost_note": "free",
        "confidence_floor": 0.5,
    },

    # ── Verification (costs credits) ───────────────────────────────────────
    {
        "code": "EMAIL_DELIVERABLE",
        "label": "Email verified deliverable",
        "description": "Email verification provider (Reoon/ZeroBounce/Hunter) confirmed mailbox exists.",
        "category": "verification",
        "value_type": "boolean",
        "collector": "verify",
        "cost_note": "Reoon/ZeroBounce/Hunter credit",
        "confidence_floor": 0.95,
    },
    {
        "code": "CONTACT_FORM_URL",
        "label": "Contact form URL",
        "description": "Personal website has a contact form. Alternative to direct email.",
        "category": "verification",
        "value_type": "text",
        "collector": "profile_scraper",
        "cost_note": "free",
        "confidence_floor": 0.8,
    },
]


async def seed_signal_catalog(state: StateManager):
    """Ensure all signal codes exist in the database."""
    await state.seed_signal_catalog(KICK_SIGNALS)


# ── Collector-to-Signal Mapping ────────────────────────────────────────────

COLLECTOR_SIGNALS = {
    "kick_api": [
        "KICK_FOLLOWERS", "KICK_CURRENT_VIEWERS", "KICK_CATEGORY",
        "KICK_PARTNER_STATUS", "KICK_HOURS_STREAMED", "KICK_STREAM_FREQUENCY",
    ],
    "profile_scraper": [
        "HAS_BUSINESS_EMAIL", "HAS_DONATION_LINK", "HAS_MERCH_LINK",
        "HAS_SPONSORSHIP_EMAIL", "USES_STREAMELEMENTS", "USES_STREAMLABS",
        "HAS_CUSTOM_OVERLAY", "RECENT_BRAND_DEAL", "HIRING_EDITOR_MOD",
        "TWITTER_DM_OPEN", "CONTACT_FORM_URL",
    ],
    "streamer_contact_finder": [
        "STREAMER_EMAIL", "MANAGER_CONTACT",
    ],
    "verify": ["EMAIL_DELIVERABLE"],
}


def get_signals_for_collector(collector: str) -> list[str]:
    """Return signal codes that a given collector produces."""
    return COLLECTOR_SIGNALS.get(collector, [])