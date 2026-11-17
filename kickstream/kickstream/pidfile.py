"""PID file + lock management — one heartbeat loop at a time."""

import os
import errno
import time
from pathlib import Path

from kickstream.paths import PID_FILE, DATA_DIR


def pid_alive(pid: int) -> bool:
    """Check if a PID is alive (cross-platform)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        return e.errno == errno.EPERM
    except Exception:
        return False


def hold_loop_lock(lock_file: str | Path) -> bool:
    """Try to acquire the loop lock. Returns True if acquired."""
    # Simple check: if PID file exists and process alive, already running
    lock_file = Path(lock_file)
    if lock_file.exists():
        try:
            pid = int(lock_file.read_text().strip())
            if pid_alive(pid):
                return False
        except (OSError, ValueError):
            pass
    # Stale lock: try to remove
    try:
        lock_file.unlink()
    except OSError:
        pass
    return True


def read_pid() -> int | None:
    if PID_FILE.exists():
        try:
            return int(PID_FILE.read_text().strip())
        except (OSError, ValueError):
            return None
    return None


def write_pid_file():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def remove_pid_file():
    try:
        PID_FILE.unlink()
    except OSError:
        pass


def release_loop_lock(lock):
    pass  # Lock is the PID file; released by remove_pid_file


def loop_lock_held(lock_file: str | Path) -> bool:
    lock_file = Path(lock_file)
    if lock_file.exists():
        try:
            pid = int(lock_file.read_text().strip())
            return pid_alive(pid)
        except (OSError, ValueError):
            return False
    return False