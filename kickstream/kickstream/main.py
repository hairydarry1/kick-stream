"""Kickstream's main heartbeat loop.

Wakes up, checks what needs doing, does it, sleeps. Repeat.
"""

import asyncio
import logging
import signal
import sys
from datetime import datetime, time, timedelta

import pytz

from kickstream.brain import Brain
from kickstream.config import (
    ConfigError, load_config, load_env, KickstreamConfig, legacy_sending_held, sending_notice,
    compliance_notice, send_window_of,
)
from kickstream.state import StateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("kickstream")


def quiet_http_client_logs():
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


quiet_http_client_logs()

# Backoff for consecutive failed cycles
ERROR_BACKOFF_BASE = 60
ERROR_BACKOFF_CAP = 900

# Hard ceiling on one cycle's parallel agents
CYCLE_TIMEOUT_SECONDS = 600

# Cap on quiet-hours sleep chunk
QUIET_HOURS_POLL_SECONDS = 300

# Phase-1 tasks that read replies
REPLY_READERS = ("handle_replies", "read_replies")

# When nothing is writable, prospecting pauses for the day
PROSPECT_BACKLOG = 20


def in_quiet_hours(config: KickstreamConfig) -> bool:
    qh = config.usage.quiet_hours
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz).time()
    start = time.fromisoformat(qh.start)
    end = time.fromisoformat(qh.end)

    if start <= end:
        return start <= now <= end
    else:
        return now >= start or now <= end


def seconds_until_quiet_hours_end(config: KickstreamConfig) -> int:
    qh = config.usage.quiet_hours
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz)
    end = time.fromisoformat(qh.end)
    end_today = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)

    if end_today <= now:
        end_today = end_today + timedelta(days=1)

    delta = end_today - now
    return max(int(delta.total_seconds()), 60)


def _new_counts(summary: dict) -> tuple[int, int]:
    prospects = summary.get("contacts") or {}
    new = (prospects.get("new", 0) if isinstance(prospects, dict) else 0) or 0
    writable = summary.get("writable_contacts", new) or 0
    return new, writable


def unusable_backlog_notice(summary: dict) -> str:
    _, writable = _new_counts(summary)
    recent = summary.get("recent_unusable_contacts", 0) or 0
    if writable or recent < PROSPECT_BACKLOG:
        return ""
    return (
        f"Kickstream found {recent} contacts in the last day, but none has a verified "
        "or catch-all email, so it paused prospecting for about a day to save search "
        "and verifier credits. Add an email verifier key (REOON_API_KEY, "
        "ZEROBOUNCE_API_KEY or HUNTER_API_KEY in .env) or check it still has credits. "
        "`kickstream export --all` exports the list as it is."
    )


async def decide_next_action(
    brain: Brain,
    state: StateManager,
    config: KickstreamConfig,
    summary: dict | None = None,
) -> str:
    if summary is None:
        summary = await state.get_state_summary()

    new_contacts, writable = _new_counts(summary)
    draft_campaigns = summary.get("draft_campaigns", 0) or 0

    email = getattr(getattr(config, "channels", None), "email", None)
    native_paused = (summary.get("sending_paused") and email.provider != "instantly")
    held = (legacy_sending_held(config) or native_paused
            or not getattr(email, "enabled", True))
    deployable = 0 if held else draft_campaigns

    if deployable > 0:
        action, reason = "send_campaign", f"{draft_campaigns} draft campaign(s) ready to deploy"
    elif writable > 0:
        action, reason = "write_campaign", f"{writable} new contact(s) with no drafts ready"
    elif unusable_backlog_notice(summary):
        action, reason = "idle", "a day's worth of contacts with no usable email"
        logger.warning(unusable_backlog_notice(summary))
    else:
        action, reason = "prospect", f"{new_contacts} new contact(s), none ready to write for"

    logger.info(f"Decision: {action} ({reason})")
    return action


async def _run_agents(tasks: list) -> dict:
    """Run (name, coroutine) pairs in parallel; return {name: outcome}."""
    if not tasks:
        return {}
    if len(tasks) > 1:
        logger.info(f"Running {len(tasks)} agents in parallel: {[t[0] for t in tasks]}")
    try:
        results = await asyncio.wait_for(
            asyncio.gather(*[t[1] for t in tasks], return_exceptions=True),
            timeout=CYCLE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error(
            f"Cycle timed out after {CYCLE_TIMEOUT_SECONDS}s with "
            f"{[t[0] for t in tasks]} still running — cancelled."
        )
        results = []
    outcomes = {}
    for (name, _), result in zip(tasks, results):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            logger.error(f"Agent {name} failed: {result}", exc_info=result)
        outcomes[name] = result
    return outcomes


async def _interruptible_sleep(seconds: float, stop_event: asyncio.Event) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def heartbeat(stop_event: asyncio.Event | None = None):
    if stop_event is None:
        stop_event = asyncio.Event()

    logger.info("=" * 60)
    logger.info("Kickstream is online.")
    logger.info("=" * 60)

    try:
        config = load_config()
    except (ConfigError, Exception) as e:
        if isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        logger.error(f"Cannot start — configuration error:\n{e}")
        return

    # Check postal_address is set (required for opt-out line in emails)
    if config.persona.postal_address == "TODO: your mailing address":
        logger.error(
            "Cannot start — postal_address is not set in kickstream.yaml.\n"
            "This is required for the opt-out line in emails.\n"
            "Please set persona.postal_address in kickstream.yaml and try again."
        )
        return

    env = load_env()
    state = StateManager()
    brain = Brain(state, config.brain)
    logger.info(
        f"Thinking with {brain.provider_label}"
        + (f" ({brain.model})" if brain.model else "")
        + ". Change brain.provider in kickstream.yaml to switch."
    )

    await state.init_db()
    logger.info("Database initialized.")

    # Import agents here to avoid circular imports
    from kickstream.agents.scout import Scout
    from kickstream.agents.writer import Writer
    from kickstream.agents.sender import Sender
    from kickstream.agents.handler import Handler
    from kickstream.agents.analyst import Analyst

    scout = Scout(brain, state, config, env)
    writer = Writer(brain, state, config, env)
    sender = Sender(brain, state, config, env)
    handler = Handler(brain, state, config, env)
    analyst = Analyst(state)

    interval = config.usage.heartbeat_interval_minutes * 60
    max_calls = max(int(200 * (config.usage.max_daily_claude_percent / 100)), 1)
    consecutive_errors = 0

    while not stop_event.is_set():
        try:
            # 1. Quiet hours
            quiet = in_quiet_hours(config)
            if quiet:
                sleep_for = seconds_until_quiet_hours_end(config)
                logger.info(f"Quiet hours. Sleeping for {sleep_for // 60} minutes.")
                shutting_down = False
                while in_quiet_hours(config):
                    if await _interruptible_sleep(QUIET_HOURS_POLL_SECONDS, stop_event):
                        shutting_down = True
                        break
                if shutting_down:
                    break
                continue

            # 2. Brain usable?
            problem = await brain.preflight()
            if problem:
                logger.error(
                    f"Kickstream is paused: {problem} Nothing was sent or changed. "
                    f"Re-checking in {config.usage.heartbeat_interval_minutes} minutes."
                )
                if await _interruptible_sleep(interval, stop_event):
                    break
                continue

            # 3. Usage budget
            if not await brain.is_within_budget(
                max_calls, max_percent=config.usage.max_daily_claude_percent
            ):
                logger.info(
                    f"{brain.provider_label} usage limit reached "
                    f"({config.usage.max_daily_claude_percent}% of quota or "
                    f"{max_calls} calls). Sleeping 1h, then re-checking."
                )
                if await _interruptible_sleep(3600, stop_event):
                    break
                continue

            # 4. Decide what to do
            logger.info("Checking pipeline state...")
            summary = await state.get_state_summary()
            action = await decide_next_action(brain, state, config, summary=summary)

            # 5. Execute
            tasks = []
            has_open_convos = (summary.get("open_conversations", 0)
                               + summary.get("auto_reply_handoffs", 0)) > 0

            if action == "prospect":
                tasks.append(("prospect", scout.run()))
                tasks.append(("analyze", analyst.run()))
            elif action == "write_campaign":
                tasks.append(("write_campaign", writer.run()))
            elif action == "idle":
                tasks.append(("analyze", analyst.run()))

            send_tasks = []
            if action == "send_campaign":
                send_tasks.append(("send_campaign", sender.run))
            else:
                send_tasks.append(("send_outbox", sender.run))
            if has_open_convos:
                tasks.append(("handle_replies", handler.run()))

            outcomes = await _run_agents(tasks)

            # Phase 2: send
            if send_tasks:
                unread = [n for n, _ in tasks
                          if n in REPLY_READERS and outcomes.get(n) is not True]
                paused = brain.paused_reason
                if paused or unread:
                    reason = paused or "replies couldn't all be read and handled."
                    logger.error(
                        f"Not sending this cycle: {reason} Queued emails wait "
                        "until replies can be read again."
                    )
                else:
                    await _run_agents([(name, run()) for name, run in send_tasks])

            # 6. Log the action
            try:
                await state.log_action(action_type=action, agent="main")
            except Exception as e:
                logger.warning(f"Failed to log action '{action}': {e}")

            consecutive_errors = 0

            # 7. Sleep until next heartbeat
            logger.info(
                f"Cycle complete. Sleeping for {config.usage.heartbeat_interval_minutes} minutes."
            )
            if await _interruptible_sleep(interval, stop_event):
                break

        except (KeyboardInterrupt, asyncio.CancelledError):
            break
        except Exception as e:
            consecutive_errors += 1
            backoff = min(
                ERROR_BACKOFF_BASE * (2 ** (consecutive_errors - 1)),
                ERROR_BACKOFF_CAP,
            )
            logger.error(
                f"Error in heartbeat (failure #{consecutive_errors}): {e}",
                exc_info=True,
            )
            logger.info(f"Recovering... sleeping {backoff}s before retry.")
            if await _interruptible_sleep(backoff, stop_event):
                break

    logger.info("Kickstream shutting down. Streamers don't clip themselves, but I need a break.")


def _needs_setup() -> bool:
    from kickstream.paths import PROJECT_ROOT
    from kickstream.config import _find_config_file, ConfigFileNotFoundError

    env_file = PROJECT_ROOT / ".env"
    if not env_file.exists():
        return True

    try:
        config_file = _find_config_file()
    except ConfigFileNotFoundError:
        return True

    try:
        import yaml
        from kickstream.textio import read_user_text
        config = yaml.safe_load(read_user_text(config_file))
        if not isinstance(config, dict):
            return True
        company = (config.get("persona") or {}).get("company", "")
        if company in ("Your Agency", ""):
            return True
    except Exception:
        return True

    return False


async def _run_with_signals():
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(sig_name: str):
        if stop_event.is_set():
            logger.info("Second shutdown signal — exiting immediately.")
            sys.exit(1)
        logger.info(f"Received {sig_name}. Finishing current work, then shutting down...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except (NotImplementedError, RuntimeError):
            signal.signal(sig, lambda s, f: _request_shutdown(signal.Signals(s).name))

    await heartbeat(stop_event)


def main():
    if _needs_setup():
        if not sys.stdin.isatty():
            print("Kickstream isn't set up yet. Run `kickstream setup` in a terminal first.")
            sys.exit(1)
        print("\n  First time running Kickstream? Let's get you set up.\n")
        from kickstream.setup import run_setup
        asyncio.run(run_setup())
        return

    from kickstream.pidfile import hold_loop_lock, write_pid_file, remove_pid_file, release_loop_lock, LOCK_FILE

    lock = hold_loop_lock(LOCK_FILE)
    if lock is None:
        print("Kickstream is already running. Stop it first (Ctrl+C in its terminal).")
        sys.exit(1)
    write_pid_file()
    try:
        asyncio.run(_run_with_signals())
    except KeyboardInterrupt:
        logger.info("Goodbye.")
    finally:
        remove_pid_file()
        release_loop_lock(lock)


if __name__ == "__main__":
    from kickstream.textio import utf8_stdio
    utf8_stdio()
    main()