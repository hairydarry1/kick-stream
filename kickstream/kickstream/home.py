"""Home folder seeding — copy bundled defaults into the user's home folder."""

import shutil
from pathlib import Path

from kickstream.paths import (
    HOME, DATA_DIR, ENV_FILE, CONFIG_FILE,
    BUNDLED_DEFAULTS_DIR, BUNDLED_CONFIG, BUNDLED_ENV_EXAMPLE,
)


def bundled_defaults() -> list[tuple[str, Path]]:
    """Return (target_relative_path, bundled_path) pairs to seed."""
    return [
        ("kickstream.yaml", BUNDLED_CONFIG),
        (".env.example", BUNDLED_ENV_EXAMPLE),
    ]


def seed_home(home_root: Path | None = None, defaults: list[tuple[str, Path]] | None = None):
    """Copy bundled defaults into the home folder if they don't exist there.

    Never overwrites user files. Creates data/ directory.
    """
    home_root = home_root or HOME
    defaults = defaults or bundled_defaults()

    home_root.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    for rel_path, bundled in defaults:
        target = home_root / rel_path
        if not target.exists() and bundled.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bundled, target)
            except OSError:
                pass  # Non-fatal: setup will prompt for these anyway