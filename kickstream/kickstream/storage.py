"""Storage abstraction — supports SQLite (default) and future backends."""

import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path

BUSY_TIMEOUT_SECONDS = 30


@asynccontextmanager
async def connect(db_path: str, timeout: int = BUSY_TIMEOUT_SECONDS):
    """Open a SQLite connection with WAL mode and busy timeout."""
    db = await aiosqlite.connect(db_path, timeout=timeout)
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.execute("PRAGMA foreign_keys=ON")
        yield db
    finally:
        await db.close()


def manages_schema(db_path: str) -> bool:
    """True if a non-SQLite backend manages its own schema."""
    # For now, only SQLite
    return False


async def check_schema(db_path: str):
    """Verify schema is compatible (for non-SQLite backends)."""
    pass