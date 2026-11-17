"""Workspace home — isolates multiple Kickstream instances (for hosted multi-tenant)."""

import contextvars
from dataclasses import dataclass
from pathlib import Path

from kickstream.paths import DB_PATH, DATA_DIR, ENV_FILE, CONFIG_FILE, LOCAL_CONFIG_FILE


@dataclass
class Home:
    root: Path
    db_path: Path
    env_file: Path
    config_file: Path
    local_config_file: Path
    data_dir: Path


_HOME: contextvars.ContextVar[Home | None] = contextvars.ContextVar("_HOME", default=None)


def use_home(home: Home):
    """Context manager to set the current home for this async context."""
    token = _HOME.set(home)
    try:
        yield
    finally:
        _HOME.reset(token)


def current_home() -> Home | None:
    return _HOME.get()


def default_home() -> Home:
    return Home(
        root=DATA_DIR.parent,
        db_path=DB_PATH,
        env_file=ENV_FILE,
        config_file=CONFIG_FILE,
        local_config_file=LOCAL_CONFIG_FILE,
        data_dir=DATA_DIR,
    )


class NotOnHosted(Exception):
    """Raised when a hosted-only operation is attempted on self-hosted."""
    def __init__(self, message: str = "This operation is only available on the hosted platform."):
        self.payload = {"error": "not_on_hosted", "message": message}
        super().__init__(message)