"""Text I/O utilities — consistent encoding, newline handling."""

from pathlib import Path


def read_user_text(path: Path | str) -> str:
    """Read a user-editable text file (config, prompts, skills).

    Always UTF-8, preserves newlines, strips BOM.
    """
    path = Path(path)
    return path.read_text(encoding="utf-8-sig")


def write_user_text(path: Path | str, content: str) -> None:
    """Write a user-editable text file atomically with 0600 perms on POSIX."""
    import os
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            if os.name != "nt":
                os.fchmod(f.fileno(), 0o600)
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def utf8_stdio() -> None:
    """Force UTF-8 stdio on Windows (Python 3.7+)."""
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")