"""Paths — where Kickstream's files live."""

import os
import sys
from pathlib import Path


def _project_root() -> Path:
    """The checkout root (this repo), not the home folder."""
    # When installed via pip, __file__ is in site-packages/kickstream/paths.py
    # The checkout is two levels up from there.
    return Path(__file__).resolve().parents[1]


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = _project_root()


def is_checkout(path: Path | str) -> bool:
    """True if `path` is inside a git checkout (has .git or pyproject.toml)."""
    path = Path(path)
    return (path / ".git").exists() or (path / "pyproject.toml").exists()


# ── Home folder (user data, config, database) ────────────────────────────

def _default_home() -> Path:
    """Default home folder per platform."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "kickstream"
    if sys.platform == "win32":
        return Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming")) / "kickstream"
    # Linux / else
    xdg = os.getenv("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "kickstream"
    return Path.home() / ".local" / "share" / "kickstream"


def get_home() -> Path:
    """Resolve the home folder: env > checkout > platform default."""
    # 1. Explicit env var wins (Docker sets this)
    if env_home := os.getenv("KICKSTREAM_HOME"):
        return Path(env_home).expanduser().resolve()

    # 2. If we're in a checkout, use it (dev mode)
    if is_checkout(PROJECT_ROOT):
        return PROJECT_ROOT

    # 3. Platform default
    return _default_home()


HOME = get_home()

# Data subfolder inside home
DATA_DIR = HOME / "data"
DB_PATH = DATA_DIR / "kickstream.db"
ENV_FILE = HOME / ".env"
CONFIG_FILE = HOME / "kickstream.yaml"
LOCAL_CONFIG_FILE = HOME / "kickstream.local.yaml"
PID_FILE = DATA_DIR / "kickstream.pid"
LOG_FILE = DATA_DIR / "kickstream.log"
TOKEN_FILE = DATA_DIR / "dashboard-token"
AGENT_TOKEN_FILE = DATA_DIR / "agent-token"

# Ensure data dir exists
DATA_DIR.mkdir(parents=True, exist_ok=True)


# ── Bundled defaults (shipped with the package) ──────────────────────────

BUNDLED_DEFAULTS_DIR = PACKAGE_ROOT / "defaults"

# These are copied to the home folder on first run if they don't exist there.
# The user edits the home-folder copies; the bundled ones stay pristine for upgrades.
BUNDLED_CONFIG = BUNDLED_DEFAULTS_DIR / "kickstream.yaml"
BUNDLED_ENV_EXAMPLE = BUNDLED_DEFAULTS_DIR / ".env.example"
BUNDLED_PROMPTS_DIR = BUNDLED_DEFAULTS_DIR / "prompts"
BUNDLED_SKILLS_DIR = BUNDLED_DEFAULTS_DIR / "skills"