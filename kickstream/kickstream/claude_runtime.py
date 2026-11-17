"""Claude Code CLI runtime — runs headless Claude calls."""

import json
import logging
import subprocess
import sys
import shutil
from pathlib import Path

logger = logging.getLogger("kickstream.claude_runtime")


class ClaudeRuntimeError(Exception):
    pass


def check_cli() -> None:
    """Verify Claude CLI is installed and supports required flags."""
    claude_path = shutil.which("claude")
    if not claude_path:
        raise ClaudeRuntimeError(
            "Claude CLI not found. Install: https://claude.ai/code\n"
            "Then run: claude login"
        )

    # Check version supports --output-format json and --dangerously-skip-permissions
    try:
        result = subprocess.run(
            [claude_path, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        help_text = result.stdout + result.stderr
        if "--output-format" not in help_text:
            raise ClaudeRuntimeError(
                "Claude CLI version too old. Update with: npm update -g @anthropic-ai/claude-code"
            )
        if "--dangerously-skip-permissions" not in help_text:
            raise ClaudeRuntimeError(
                "Claude CLI version too old. Update with: npm update -g @anthropic-ai/claude-code"
            )
    except subprocess.TimeoutExpired:
        raise ClaudeRuntimeError("Claude CLI --help timed out")
    except FileNotFoundError:
        raise ClaudeRuntimeError("Claude CLI not found in PATH")


def fingerprint() -> str:
    """Return a fingerprint of the Claude CLI installation for change detection."""
    claude_path = shutil.which("claude") or ""
    try:
        result = subprocess.run(
            [claude_path, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        version = result.stdout.strip() or result.stderr.strip()
        return f"{claude_path}|{version}"
    except Exception:
        return claude_path


def run_claude(cmd: list[str], timeout: int = 300) -> dict:
    """Run a Claude CLI command and return parsed JSON result."""
    logger.debug(f"Running: {' '.join(cmd[:3])} ... (prompt omitted)")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as e:
        raise ClaudeRuntimeError(f"Claude CLI timed out after {timeout}s")
    except FileNotFoundError:
        raise ClaudeRuntimeError("Claude CLI not found")
    except Exception as e:
        raise ClaudeRuntimeError(f"Failed to run Claude CLI: {e}")

    if result.returncode != 0:
        stderr = result.stderr.strip()[:500]
        stdout = result.stdout.strip()[:500]
        raise ClaudeRuntimeError(f"Claude CLI exited {result.returncode}: {stderr or stdout}")

    # Parse JSON output
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        # Sometimes there's extra output before/after JSON
        stdout = result.stdout.strip()
        # Try to find JSON object
        import re
        match = re.search(r"\{.*\}", stdout, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        raise ClaudeRuntimeError(f"Claude CLI returned non-JSON: {stdout[:200]}")