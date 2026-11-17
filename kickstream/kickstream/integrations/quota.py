"""Claude quota client — reads subscription utilization from local Claude Code data."""

import json
import logging
import os
import subprocess
import time
from pathlib import Path

logger = logging.getLogger("kickstream.quota")


class QuotaClient:
    """Reads Claude Pro/Max quota utilization from the local CLI's storage."""

    def __init__(self):
        self._cache: dict | None = None
        self._cache_at = 0.0
        self._cache_ttl = 60  # seconds

    def _claude_data_dir(self) -> Path:
        """Find Claude's data directory."""
        # Check common locations
        candidates = [
            Path.home() / ".claude",
            Path.home() / ".config" / "claude",
            Path(os.getenv("APPDATA", "")) / "claude" if os.getenv("APPDATA") else None,
        ]
        for c in candidates:
            if c and c.exists():
                return c
        return Path.home() / ".claude"  # default

    async def get_utilization(self) -> dict | None:
        """Get current quota utilization windows.

        Returns dict with keys like 'five_hour', 'seven_day', 'limit_reached'
        each containing 'utilization' (0-100), 'resets_at' (ISO timestamp).
        """
        now = time.monotonic()
        if self._cache and (now - self._cache_at) < self._cache_ttl:
            return self._cache

        # Try to read from Claude's local data
        data = await self._read_quota_from_claude()
        if data:
            self._cache = data
            self._cache_at = now
            return data

        # Fallback: try claude /usage command (if available)
        data = await self._read_quota_from_cli()
        if data:
            self._cache = data
            self._cache_at = now
            return data

        return None

    async def _read_quota_from_claude(self) -> dict | None:
        """Read quota from Claude's local storage files."""
        try:
            data_dir = self._claude_data_dir()
            # Look for quota/usage files
            quota_file = data_dir / "quota.json"
            if quota_file.exists():
                import aiofiles
                async with aiofiles.open(quota_file, "r") as f:
                    content = await f.read()
                    return json.loads(content)
        except Exception as e:
            logger.debug(f"Could not read Claude quota file: {e}")
        return None

    async def _read_quota_from_cli(self) -> dict | None:
        """Run `claude /usage` or similar to get quota."""
        try:
            # This is a placeholder - actual implementation depends on CLI capabilities
            # For now, return None to indicate unavailable
            return None
        except Exception as e:
            logger.debug(f"CLI quota check failed: {e}")
        return None