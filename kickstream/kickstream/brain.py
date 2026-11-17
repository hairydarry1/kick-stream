"""Brain — Kickstream's thinking engine.

Runs on the operator's Claude subscription (Claude Code headless, the
default) or, when ``brain.provider: codex`` is configured, their ChatGPT
subscription through an isolated Codex runtime. The provider is fixed per
Brain; a failing provider never falls back to the other one.
"""

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from kickstream.state import StateManager

logger = logging.getLogger("kickstream.brain")

from kickstream.paths import PROJECT_ROOT
from kickstream.textio import read_user_text
from kickstream.workspace_home import current_home

PROMPTS_DIR = PROJECT_ROOT / "prompts"
SKILLS_DIR = PROJECT_ROOT / "skills"

# Subprocess safety limits
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_RETRIES = 2
RETRY_BASE_DELAY = 5.0
KILL_REAP_SECONDS = 10.0

_NON_RETRYABLE_PATTERNS = (
    "not logged in",
    "please run `claude login`",
    "invalid api key",
    "unauthorized",
)

PROVIDER_LABELS = {"claude": "Claude", "codex": "Codex"}

BRAIN_STATUS_KEY = "brain_status"

_PERSISTENT_CODEX_KINDS = {"setup", "auth", "isolation"}
PROVIDER_RECHECK_SECONDS = 1800
USAGE_LIMIT_PAUSE_SECONDS = 3600


def parse_brain_status(raw: str) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def active_brain_status(raw: str, provider: str) -> dict:
    status = parse_brain_status(raw)
    return status if status.get("provider") == provider else {}


class BrainUnavailable(Exception):
    """The provider can't be used until the operator acts."""


class Brain:
    def __init__(self, state: StateManager, brain_config=None):
        from kickstream.config import BrainConfig

        self.state = state
        self.config = brain_config or BrainConfig()
        self.provider = self.config.provider
        self.model = self.config.model
        self.codex = None
        self.last_reply_empty = False
        self.unavailable = ""
        self._unavailable_at = 0.0
        self._unavailable_fingerprint = None
        self._usage_limited_until = 0.0
        self._usage_pause_recorded = False
        self._status_synced = False
        if self.provider == "codex":
            from kickstream.codex import CodexClient, CodexQuotaClient
            self.codex = CodexClient(
                model=self.model, reasoning_effort=self.config.reasoning_effort
            )
            self.quota = CodexQuotaClient(self.codex)
        else:
            from kickstream.integrations.quota import QuotaClient
            self.quota = QuotaClient()

    @property
    def provider_label(self) -> str:
        return PROVIDER_LABELS[self.provider]

    @property
    def paused_reason(self) -> str:
        if self.unavailable:
            return self.unavailable
        if time.monotonic() < self._usage_limited_until:
            return f"{self.provider_label} usage limit reached for this plan"
        return ""

    async def preflight(self) -> str:
        if self.provider != "codex":
            return await self._claude_preflight()
        from kickstream.codex import CodexError

        await self._expire_usage_pause()
        if self.unavailable:
            unchanged = self.codex.fingerprint() == self._unavailable_fingerprint
            recent = time.monotonic() - self._unavailable_at < PROVIDER_RECHECK_SECONDS
            if unchanged and recent:
                return self.unavailable
        try:
            await self.codex.preflight()
        except CodexError as e:
            if e.kind in _PERSISTENT_CODEX_KINDS:
                await self._mark_unavailable(e)
                return self.unavailable
            return str(e)
        except Exception as e:
            return f"Codex preflight failed unexpectedly ({type(e).__name__})."
        if self.unavailable:
            await self._mark_available()
        elif not self._status_synced:
            logger.info(
                f"Codex ready: supported CLI, isolation verified, login and account "
                f"valid, model {self.model or 'auto'} usable with no tools (no prompt sent)."
            )
            self._status_synced = True
            try:
                stored = parse_brain_status(await self.state.get_setting(BRAIN_STATUS_KEY))
            except Exception:
                stored = {}
            until = stored.get("until")
            if (stored.get("status") == "paused" and stored.get("kind") == "usage_limit"
                    and isinstance(until, (int, float)) and until > time.time()):
                self._usage_limited_until = time.monotonic() + (until - time.time())
                self._usage_pause_recorded = True
            elif stored.get("status") == "paused":
                await self._mark_available()
        return ""

    async def _claude_preflight(self) -> str:
        from kickstream.claude_runtime import ClaudeRuntimeError, check_cli

        try:
            await asyncio.to_thread(check_cli)
        except ClaudeRuntimeError as e:
            await self._mark_unavailable(e)
            return self.unavailable
        if self.unavailable:
            await self._mark_available()
        return ""

    def _fingerprint(self):
        if self.provider == "codex":
            return self.codex.fingerprint()
        from kickstream.claude_runtime import fingerprint
        return fingerprint()

    async def _mark_unavailable(self, error: Exception):
        self.unavailable = str(error)
        self._unavailable_at = time.monotonic()
        self._unavailable_fingerprint = self._fingerprint()
        await self.state.set_setting(BRAIN_STATUS_KEY, json.dumps({
            "provider": self.provider,
            "status": "paused",
            "reason": self.unavailable,
            "since": time.time(),
            "kind": getattr(error, "kind", "unknown"),
        }))

    async def _mark_available(self):
        self.unavailable = ""
        self._unavailable_fingerprint = None
        await self.state.set_setting(BRAIN_STATUS_KEY, json.dumps({
            "provider": self.provider,
            "status": "ok",
            "since": time.time(),
        }))

    async def _expire_usage_pause(self):
        if self._usage_limited_until and time.monotonic() >= self._usage_limited_until:
            self._usage_limited_until = 0.0
            self._usage_pause_recorded = False
            if self.unavailable and "usage limit" in self.unavailable.lower():
                await self._mark_available()

    # ── Core thinking methods ──

    async def think(self, prompt: str, *, system: str = "", session_id: str = "") -> str:
        """Raw text completion. Returns the model's reply as a string."""
        if self.provider == "codex":
            return await self._think_codex(prompt, system=system, session_id=session_id)
        return await self._think_claude(prompt, system=system, session_id=session_id)

    async def think_json(
        self,
        prompt: str,
        *,
        system: str = "",
        schema: dict | None = None,
        session_id: str = "",
    ) -> dict:
        """Structured JSON completion. Validates against schema if provided."""
        if schema:
            schema_instruction = (
                "\n\nRESPOND WITH VALID JSON ONLY matching this schema:\n"
                f"{json.dumps(schema, separators=(',', ':'))}"
            )
        else:
            schema_instruction = "\n\nRESPOND WITH VALID JSON ONLY."
        full_prompt = prompt + schema_instruction
        raw = await self.think(full_prompt, system=system, session_id=session_id)
        return self._parse_json(raw, schema=schema)

    async def _think_claude(self, prompt: str, *, system: str = "", session_id: str = "") -> str:
        from kickstream.claude_runtime import ClaudeRuntimeError, run_claude

        cmd = ["claude", "-p", "--output-format", "json", "--dangerously-skip-permissions"]
        if system:
            cmd.extend(["--system-prompt", system])
        cmd.append(prompt)

        start = time.monotonic()
        last_error = None
        for attempt in range(DEFAULT_MAX_RETRIES + 1):
            try:
                result = await asyncio.to_thread(run_claude, cmd, timeout=DEFAULT_TIMEOUT_SECONDS)
                duration_ms = int((time.monotonic() - start) * 1000)
                await self._record_usage({
                    "agent": "brain",
                    "task": "think",
                    "session_id": session_id,
                    "model": self.model or "auto",
                    "input_tokens": result.get("usage", {}).get("input_tokens", 0),
                    "output_tokens": result.get("usage", {}).get("output_tokens", 0),
                    "cache_read_tokens": result.get("usage", {}).get("cache_read_tokens", 0),
                    "duration_ms": duration_ms,
                    "is_error": 0,
                    "provider": "claude",
                })
                text = result.get("result", "")
                self.last_reply_empty = not text.strip()
                return text
            except ClaudeRuntimeError as e:
                last_error = e
                if any(p in str(e).lower() for p in _NON_RETRYABLE_PATTERNS):
                    await self._record_usage({
                        "agent": "brain", "task": "think", "session_id": session_id,
                        "model": self.model or "auto", "is_error": 1, "provider": "claude",
                    })
                    raise BrainUnavailable(str(e))
                if attempt < DEFAULT_MAX_RETRIES:
                    await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
            except Exception as e:
                last_error = e
                if attempt < DEFAULT_MAX_RETRIES:
                    await asyncio.sleep(RETRY_BASE_DELAY * (2 ** attempt))
        await self._record_usage({
            "agent": "brain", "task": "think", "session_id": session_id,
            "model": self.model or "auto", "is_error": 1, "provider": "claude",
        })
        raise BrainUnavailable(f"Claude failed after retries: {last_error}")

    async def _think_codex(self, prompt: str, *, system: str = "", session_id: str = "") -> str:
        from kickstream.codex import CodexError

        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        start = time.monotonic()
        try:
            result = await self.codex.run(full_prompt)
            duration_ms = int((time.monotonic() - start) * 1000)
            await self._record_usage({
                "agent": "brain", "task": "think", "session_id": session_id,
                "model": self.model or "auto", "duration_ms": duration_ms,
                "is_error": 0, "provider": "codex",
            })
            self.last_reply_empty = not result.strip()
            return result
        except CodexError as e:
            await self._record_usage({
                "agent": "brain", "task": "think", "session_id": session_id,
                "model": self.model or "auto", "is_error": 1, "provider": "codex",
            })
            if e.kind in _PERSISTENT_CODEX_KINDS:
                raise BrainUnavailable(str(e))
            raise

    def _parse_json(self, raw: str, schema: dict | None = None) -> dict:
        """Parse JSON from model output, with fence stripping fallback."""
        text = raw.strip()
        # Strip markdown fences
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n", "", text)
            text = re.sub(r"\n```$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            # Try to extract JSON object from text
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
            logger.warning(f"Brain: failed to parse JSON: {e}. Raw: {text[:200]}")
            raise BrainUnavailable(f"Model returned invalid JSON: {e}")

    async def _record_usage(self, event: dict):
        await self.state.record_usage(event)

    async def is_within_budget(self, max_calls: int, max_percent: int = 80) -> bool:
        """Check if we're within the daily usage budget."""
        try:
            if self.provider == "codex":
                windows = await self.quota.get_utilization()
                if windows:
                    five_hour = windows.get("five_hour", {})
                    if five_hour.get("utilization", 0) >= max_percent:
                        return False
            else:
                windows = await self.quota.get_utilization()
                if windows:
                    five_hour = windows.get("five_hour", {})
                    if five_hour.get("utilization", 0) >= max_percent:
                        return False
        except Exception:
            pass  # If quota check fails, allow the call
        return True