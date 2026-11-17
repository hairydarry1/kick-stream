"""Configuration models for Kickstream."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ── Environment (secrets) ─────────────────────────────────────────────────


class EnvConfig(BaseSettings):
    """Secrets from .env — never logged, never committed."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Email provider (choose one)
    GMAIL_CLIENT_ID: str = ""
    GMAIL_CLIENT_SECRET: str = ""
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    IMAP_HOST: str = ""
    IMAP_PORT: int = 993
    IMAP_USERNAME: str = ""
    IMAP_PASSWORD: str = ""

    # Email verification (at least one recommended)
    REOON_API_KEY: str = ""
    ZEROBOUNCE_API_KEY: str = ""
    HUNTER_API_KEY: str = ""

    # Optional: LinkedIn (for future expansion)
    LINKEDIN_EMAIL: str = ""
    LINKEDIN_PASSWORD: str = ""

    # Optional: Cloudflare (for JS rendering)
    CLOUDFLARE_ACCOUNT_ID: str = ""
    CLOUDFLARE_API_TOKEN: str = ""

    # Optional: TrulyInbox warmup
    TRULYINBOX_API_KEY: str = ""
    TRULYINBOX_WORKSPACE_ID: str = ""

    # Optional: Precleared compliance
    PRECLEARED_API_KEY: str = ""


def load_env() -> EnvConfig:
    """Load environment config from home folder's .env."""
    from kickstream.paths import ENV_FILE
    return EnvConfig(_env_file=str(ENV_FILE), _env_file_encoding="utf-8")


# ── Brain (LLM provider) ──────────────────────────────────────────────────


class BrainConfig(BaseModel):
    provider: Literal["claude", "codex"] = "claude"
    model: str = ""
    reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "medium"

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, v: str) -> str:
        if v not in ("claude", "codex"):
            raise ValueError("provider must be 'claude' or 'codex'")
        return v


# ── Usage / Scheduling ────────────────────────────────────────────────────


class QuietHours(BaseModel):
    start: str = "22:00"  # HH:MM
    end: str = "07:00"
    timezone: str = "America/New_York"

    @field_validator("start", "end")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        try:
            h, m = map(int, v.split(":"))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
        except Exception:
            raise ValueError("Time must be HH:MM (24-hour)")
        return v


class WarmupConfig(BaseModel):
    start_date: str = ""  # YYYY-MM-DD
    dkim_selector: str = "default"


class EmailChannel(BaseModel):
    provider: Literal["gmail", "smtp"] = "gmail"
    enabled: bool = True
    require_approval: bool = True
    min_write_score: int = 0
    max_daily_sends: int = 20
    warmup: WarmupConfig = Field(default_factory=WarmupConfig)
    link_domains: list[str] = Field(default_factory=list)  # allowed domains in emails


class Channels(BaseModel):
    email: EmailChannel = Field(default_factory=EmailChannel)


class UsageConfig(BaseModel):
    heartbeat_interval_minutes: int = 15
    max_daily_claude_percent: int = 80
    quiet_hours: QuietHours = Field(default_factory=QuietHours)


# ── ICP (Ideal Customer Profile) ─────────────────────────────────────────


class ICPConfig(BaseModel):
    # Kick streamer targeting
    categories: list[str] = Field(
        default_factory=lambda: ["Just Chatting", "Gaming", "Music", "Art", "IRL"]
    )
    min_followers: int = 500
    max_followers: int = 50000
    min_viewers: int = 0  # Current live viewers (from /livestreams)
    max_viewers: int = 0  # Current live viewers (from /livestreams)
    partner_status: list[str] = Field(default_factory=lambda: ["affiliate", "partner", "none"])
    geography: list[str] = Field(default_factory=lambda: ["US", "CA", "AU"])  # US/CA/AU only

    # Hiring signals (for future: streamers hiring editors/mods)
    hiring_signals: list[str] = Field(default_factory=list)


# ── Persona / Product ─────────────────────────────────────────────────────


class PersonaConfig(BaseModel):
    company: str = "Your Agency"
    name: str = "Your Name"
    email: str = "you@yourdomain.com"
    title: str = "Founder"
    website: str = "https://your-agency.com"
    postal_address: str = "TODO: your mailing address"

    @field_validator("postal_address")
    @classmethod
    def _validate_postal_address(cls, v: str) -> str:
        if v == "TODO: your mailing address" or not v.strip():
            raise ValueError(
                "postal_address must be set for compliance (opt-out line in emails). "
                "Please set persona.postal_address in kickstream.yaml."
            )
        return v


class ProductConfig(BaseModel):
    name: str = "Streamer Services"
    description: str = "Services for Kick streamers including clipping, social distribution, OBS setup, and visual assets."
    price: str = "TODO: pricing"
    benefits: list[str] = Field(default_factory=lambda: [
        "TODO: benefit 1",
        "TODO: benefit 2",
        "TODO: benefit 3",
        "TODO: benefit 4",
    ])
    primary_offer: str = "TODO: primary offer"
    low_commitment_entry: str = "TODO: low-commitment entry"
    goal: str = "TODO: goal"
    meeting_length_minutes: int = 15
    meeting_booker: str = "TODO: meeting booker"


# ── Compliance ────────────────────────────────────────────────────────────


class PreclearedConfig(BaseModel):
    enabled: bool = False
    sender_country: str = "US"
    prospect_country: str = ""
    address_source: str = "unknown"


class ComplianceConfig(BaseModel):
    precleared: PreclearedConfig = Field(default_factory=PreclearedConfig)
    do_not_contact: list[str] = Field(default_factory=list)  # domains


# ── Root Config ───────────────────────────────────────────────────────────


class KickstreamConfig(BaseModel):
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    product: ProductConfig = Field(default_factory=ProductConfig)
    icp: ICPConfig = Field(default_factory=ICPConfig)
    usage: UsageConfig = Field(default_factory=UsageConfig)
    channels: Channels = Field(default_factory=Channels)
    brain: BrainConfig = Field(default_factory=BrainConfig)
    compliance: ComplianceConfig = Field(default_factory=ComplianceConfig)


# ── Config Loading ────────────────────────────────────────────────────────


class ConfigFileNotFoundError(Exception):
    pass


def _find_config_file() -> Path:
    """Find the active config file: local override wins over tracked."""
    from kickstream.paths import LOCAL_CONFIG_FILE, CONFIG_FILE
    if LOCAL_CONFIG_FILE.exists():
        return LOCAL_CONFIG_FILE
    if CONFIG_FILE.exists():
        return CONFIG_FILE
    raise ConfigFileNotFoundError(
        f"Neither {LOCAL_CONFIG_FILE} nor {CONFIG_FILE} exists. Run `kickstream setup` or `kickstream train <url>`."
    )


def load_config() -> KickstreamConfig:
    """Load config from the active config file (local override wins)."""
    import yaml
    from kickstream.textio import read_user_text

    config_file = _find_config_file()
    raw = yaml.safe_load(read_user_text(config_file))
    if not isinstance(raw, dict):
        raise ValueError(f"Config file {config_file} must contain a YAML mapping")
    return KickstreamConfig(**raw)


def load_brain_config() -> BrainConfig:
    """Load just the brain config (for trainer, etc.)."""
    return load_config().brain


# ── Helper functions for dashboard / CLI ─────────────────────────────────


def sending_notice(config: KickstreamConfig) -> str:
    """Message shown when sending is held by config (not pause)."""
    email = config.channels.email
    if not email.enabled:
        return "Email channel is disabled in config."
    if email.provider not in ("gmail", "smtp"):
        return ""
    return ""


def compliance_notice(config: KickstreamConfig) -> str:
    """Message shown when postal address is missing."""
    return ""


def send_window_of(config: KickstreamConfig):
    """Return send window config or None."""
    email = config.channels.email
    if email.provider not in ("gmail", "smtp"):
        return None
    # Simplified: no per-recipient window for MVP
    return None


def legacy_sending_held(config: KickstreamConfig) -> bool:
    """Back-compat: Instantly doesn't have native pause."""
    return False