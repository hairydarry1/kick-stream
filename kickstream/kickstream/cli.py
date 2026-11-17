"""Kickstream CLI — commands to install, setup, run, and manage Kickstream."""

import argparse
import asyncio
import subprocess
import sys
import textwrap
from pathlib import Path


MIN_PYTHON = (3, 11)


def _check_python_version():
    if sys.version_info >= MIN_PYTHON:
        return
    have = f"{sys.version_info.major}.{sys.version_info.minor}"
    want = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    print(f"\n  Kickstream needs Python {want} or newer. This is Python {have}.")
    print(f"  ({sys.executable})\n")
    if sys.platform == "darwin":
        print("  On macOS, `python3` is usually the system 3.9, so a venv built")
        print("  with it is 3.9 too. Build the venv from a newer Python instead:\n")
        print("    brew install python@3.13")
        print("    rm -rf .venv")
        print("    $(brew --prefix)/bin/python3.13 -m venv .venv")
        print("    source .venv/bin/activate && pip install -e .\n")
    sys.exit(1)


def cmd_install(args):
    """Install all dependencies including Playwright browsers."""
    _check_python_version()
    print("\n  Installing Kickstream dependencies...\n")

    from kickstream.paths import PACKAGE_ROOT, is_checkout
    checkout = is_checkout(PACKAGE_ROOT)
    if checkout:
        print("  [1/2] Installing Python packages...")
        requirements = PACKAGE_ROOT / "requirements.txt"
        pip_args = ["-r", "requirements.txt"] if requirements.exists() else ["-e", "."]
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", *pip_args],
            cwd=str(PACKAGE_ROOT),
        )
        if result.returncode != 0:
            print("\n  Failed to install Python packages.")
            print("  Tip: make sure you're inside a virtualenv "
                  "(python3 -m venv .venv && source .venv/bin/activate).")
            sys.exit(1)
        print("  ✓ Python packages installed.\n")
    else:
        print("  [1/2] Python packages: already part of this install.")

    print("  [2/2] Installing Playwright browsers...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            timeout=600,
        )
        playwright_ok = result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        playwright_ok = False
    if not playwright_ok:
        print("\n  Playwright browser install failed (optional — needed for profile scraping).")
    else:
        print("  ✓ Playwright browsers installed.\n")

    if checkout:
        _ensure_importable(str(PACKAGE_ROOT))
    print("  Kickstream is installed. Run 'kickstream setup' next.\n")


def _ensure_importable(project_root: str):
    check = subprocess.run(
        [sys.executable, "-c", "import kickstream"],
        cwd="/", capture_output=True,
    )
    if check.returncode == 0:
        return
    try:
        import site
        site_packages = Path(site.getsitepackages()[0])
        link = site_packages / "kickstream"
        target = Path(project_root) / "kickstream"
        if not link.exists() and target.is_dir():
            link.symlink_to(target)
            recheck = subprocess.run(
                [sys.executable, "-c", "import kickstream"],
                cwd="/", capture_output=True,
            )
            if recheck.returncode == 0:
                print("  ✓ Fixed package visibility (editable .pth was being "
                      "ignored; linked the package directly).\n")
                return
    except OSError as e:
        print(f"  Could not apply import fix: {e}")
    print("\n  Warning: 'import kickstream' fails outside the project directory.")
    print("  Run kickstream commands from the project root, or reinstall with:")
    print("    pip install -e . --config-settings editable_mode=compat\n")


def cmd_setup(args):
    """Run the interactive setup wizard."""
    from kickstream.setup import run_setup
    asyncio.run(run_setup())


def cmd_run(args):
    """Start Kickstream's heartbeat loop."""
    from kickstream.main import main
    main()


def cmd_dashboard(args):
    """Launch the local web dashboard."""
    from kickstream.dashboard import DashboardSetupError, dashboard_url, start_dashboard
    from kickstream.dashboard_auth import TokenError

    if not 1 <= args.port <= 65535:
        print(f"  Invalid port: {args.port}. Must be 1-65535.")
        sys.exit(2)

    try:
        if getattr(args, "url", False):
            print(dashboard_url(host=args.host, port=args.port))
            return
        start_dashboard(host=args.host, port=args.port)
    except (DashboardSetupError, TokenError) as e:
        print(f"\n  Dashboard not started: {e}\n")
        sys.exit(2)


def cmd_status(args):
    """Show current pipeline status."""
    from kickstream.state import StateManager

    async def _status():
        state = StateManager()
        await state.init_db()
        summary = await state.get_state_summary()

        print("\n  Kickstream Pipeline Status")
        print("  " + "=" * 40)
        print(f"  Streamers:            {summary.get('streamers', 0)}")
        prospects = summary.get("prospects") or {}
        print(f"  Contacts:             {sum(prospects.values())}")
        print(f"  New contacts:         {prospects.get('new', 0)}")
        print(f"  Writable contacts:    {summary.get('writable_prospects', 0)}")
        print(f"  Draft campaigns:      {summary.get('draft_campaigns', 0)}")
        print(f"  Active campaigns:     {summary.get('active_campaigns', 0)}")
        print(f"  Open conversations:   {summary.get('open_conversations', 0)}")
        print(f"  Claude calls today:   {summary.get('usage_today', 0)}")
        print()

    asyncio.run(_status())


def cmd_discover(args):
    """Find Kick streamers via Kick API."""
    from kickstream.state import StateManager
    from kickstream.config import load_config
    from kickstream.collectors.discover import run_discovery

    async def _discover():
        config = load_config()
        state = StateManager()
        await state.init_db()

        result = await run_discovery(
            state, config,
            category=args.category,
            max_streamers=args.max,
            min_followers=args.min_followers,
            max_followers=args.max_followers,
            min_viewers=args.min_viewers,
            max_viewers=args.max_viewers,
        )
        print(f"\n  DISCOVER — {result.get('found', 0)} streamers found")
        print(f"    {result.get('new_streamers', 0)} new, {result.get('known_streamers', 0)} already known")
        print(f"    {result.get('observations', 0)} observations recorded")
        if result.get("errors"):
            for err in result["errors"][:5]:
                print(f"    ! {err}")
        print("\n  Next: kickstream profile (scrape profiles for buying signals)")
        print("        kickstream signals --confirm KICK_CURRENT_VIEWERS,HAS_BUSINESS_EMAIL\n")

    asyncio.run(_discover())


def cmd_profile(args):
    """Scrape streamer profiles for buying signals (free)."""
    from kickstream.state import StateManager
    from kickstream.config import load_config
    from kickstream.collectors.profile import run_profile_stage

    async def _profile():
        state = StateManager()
        await state.init_db()

        pending = await state.count_streamers_needing_profile(args.stale_days)
        if not pending:
            print("\n  Nothing to profile. Every streamer has been scraped recently.\n")
            return

        print(f"\n  {pending} streamers need profiling. Scraping up to {args.limit}...\n")
        config = load_config()
        streamers, observations, _ = await run_profile_stage(
            state, limit=args.limit, stale_days=args.stale_days, config=config
        )
        print(f"  Scraped {streamers} profiles → {observations} observations (free)")
        print("\n  See what turned up: kickstream dashboard → Signals\n")

    asyncio.run(_profile())


def cmd_signals(args):
    """Review/confirm which signals Kickstream prospects against."""
    from kickstream.state import StateManager
    from kickstream.signals import seed_signal_catalog

    async def _signals():
        state = StateManager()
        await state.init_db()
        await seed_signal_catalog(state)

        codes = [a.strip().upper() for a in (args.confirm or args.reject or "").split(",") if a.strip()]
        if codes:
            status = "confirmed" if args.confirm else "rejected"
            if codes == ["ALL"]:
                codes = [c["code"] for c in await state.get_signal_codes()]
            elif codes == ["FREE"]:
                codes = [c["code"] for c in await state.get_signal_codes()
                         if (c["cost_note"] or "").lower().startswith(("free", "included"))]
            changed = sum(
                [1 for c in codes if await state.set_signal_status(c, status)]
            )
            print(f"\n  {changed} signal(s) {status}.\n")
            return

        rows = await state.get_signal_codes()
        counts = {c["signal_code"]: c for c in await state.signal_counts()}
        by_status = {"confirmed": 0, "proposed": 0, "rejected": 0}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1

        print(f"\n  Signals — {by_status['confirmed']} confirmed, "
              f"{by_status['proposed']} awaiting you, {by_status['rejected']} off")
        print("  " + "=" * 66)
        mark = {"confirmed": "[on] ", "proposed": "[ ? ]", "rejected": "[off]"}
        current = None
        for r in rows:
            if r["category"] != current:
                current = r["category"]
                print(f"\n  {current.upper()}")
            seen = counts.get(r["code"], {}).get("companies", 0)
            seen_txt = f"  ({seen} streamers)" if seen else ""
            print(f"    {mark.get(r['status'], '     ')} {r['code']:<30} "
                  f"{r['label']}{seen_txt}")
            print(f"          {r['cost_note']}")

        if by_status["proposed"]:
            print("\n  Confirm with: kickstream signals --confirm CODE[,CODE...]")
            print("  Everything free: kickstream signals --confirm free")
            print("  Details: kickstream dashboard → Signals\n")
        else:
            print("")

    asyncio.run(_signals())


def cmd_export(args):
    """Export the contact list as a sequencer-ready CSV."""
    from kickstream.state import StateManager
    from kickstream.export import export_contacts_csv

    async def _export():
        state = StateManager()
        await state.init_db()

        email_statuses = None
        if args.email_status:
            email_statuses = [s.strip() for s in args.email_status.split(",") if s.strip()]
        statuses = None
        if args.status:
            statuses = [s.strip() for s in args.status.split(",") if s.strip()]

        count, _ = await export_contacts_csv(
            state,
            out_path=args.out,
            email_statuses=email_statuses,
            min_score=args.min_score,
            statuses=statuses,
            include_all=args.all,
        )
        scope = "all contacts" if args.all else (
            f"email status {email_statuses or ['verified', 'risky']}"
            + (f", score >= {args.min_score}" if args.min_score else "")
        )
        print(f"\n  Exported {count} contact(s) to {args.out}  ({scope})")
        if count == 0 and not args.all:
            print("  Tip: no deliverable emails yet? Add a REOON_API_KEY to .env so")
            print("  Kickstream can verify addresses, or use --all for the raw list.\n")
        else:
            print("  The CSV imports directly into any email sequencer.\n")

    asyncio.run(_export())


def cmd_outbox(args):
    """Review and approve queued outgoing emails."""
    from kickstream.state import StateManager

    async def _outbox():
        state = StateManager()
        await state.init_db()

        if args.approve_all:
            n = await state.approve_outbox()
            print(f"\n  Approved {n} email(s). They'll send on schedule.")
            held = await state.count_held_for_review()
            if held:
                print(f"  {held} email(s) held for review were left out: read each one,")
                print("  then approve it with --approve <id>.")
            print()
            return
        if args.approve:
            n = await state.approve_outbox(args.approve)
            print(f"\n  {'Approved.' if n else 'No pending item with that id.'}\n")
            return
        if args.reject:
            n = await state.reject_outbox_item(args.reject)
            print(f"\n  {'Rejected.' if n else 'Nothing waiting to send with that id.'}\n")
            return

        paused = await state.get_setting("sending_paused")
        if paused:
            print(f"\n  ⚠ SENDING PAUSED: {paused}")
            print("  Resume with: kickstream sending resume")

        pending = await state.get_outbox(status="pending_review", limit=50)
        approved = await state.get_outbox(status="approved", limit=10)
        print(f"\n  Outbox — {len(pending)} awaiting approval, {len(approved)}+ approved/scheduled")
        print("  " + "=" * 60)
        for item in pending:
            print(f"\n  [{item['id']}] step {item['step']} ({item['kind']}) "
                  f"→ {item['to_email']}  (send {item['send_at'][:16]})")
            print(f"  Subject: {item['subject']}")
            body_preview = (item["body"][:200] + "...") if len(item["body"]) > 200 else item["body"]
            for line in body_preview.splitlines():
                print(f"    {line}")
        if pending:
            print("\n  Approve: kickstream outbox --approve <id>   |   all: kickstream outbox --approve-all")
            print("  Reject:  kickstream outbox --reject <id>\n")
        else:
            print("  Nothing awaiting review.\n")

    asyncio.run(_outbox())


def cmd_sending(args):
    """Pause/resume the sending kill switch."""
    from kickstream.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        if args.sending_action == "pause":
            await state.set_setting("sending_paused", "paused manually")
            print("\n  Sending paused. Nothing will leave the outbox.\n")
        elif args.sending_action == "resume":
            await state.set_setting("sending_paused", "")
            print("\n  Sending resumed.\n")
        else:
            paused = await state.get_setting("sending_paused")
            print(f"\n  Sending: {'PAUSED — ' + paused if paused else 'active'}\n")

    asyncio.run(_run())


def cmd_warmup(args):
    """Check whether the sending domain is ready for cold email."""
    import os
    from dotenv import load_dotenv
    from kickstream import deliverability as d
    from kickstream.config import ConfigError, load_config
    from kickstream.paths import PROJECT_ROOT
    from kickstream.state import StateManager

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        config = load_config()
    except ConfigError as e:
        print(f"\n  {e}\n")
        raise SystemExit(1)
    email = config.channels.email
    smtp_user = os.getenv("SMTP_USERNAME", "").strip() if email.provider == "smtp" else ""
    sender = smtp_user if "@" in smtp_user else config.persona.email
    domain = (args.domain or sender.rpartition("@")[2]).strip().lower().rstrip(".")
    if not d.DOMAIN_RE.match(domain):
        print("\n  No valid sending domain found. Pass one: kickstream warmup yourdomain.com\n")
        raise SystemExit(1)

    async def _history():
        state = StateManager()
        await state.init_db()
        return (await state.count_outbox_sent(),
                0)  # bounce count not tracked yet

    smtp_host = os.getenv("SMTP_HOST", "").strip() if email.provider == "smtp" else ""
    print(f"\n  Deliverability check - {domain}")
    print("  " + "=" * 56)
    sections = [
        ("Authentication", [d.check_mx(domain), d.check_spf(domain),
                            d.check_dkim(domain, email.warmup.dkim_selector),
                            d.check_dmarc(domain)]),
        ("Reputation", [d.check_domain_age(domain), *d.check_domain_blocklists(domain)]
         + (d.check_ip_blocklists(smtp_host) if smtp_host else [])),
        ("Warmup & sending", [d.check_warmup(email.warmup, email.max_daily_sends,
                                             provider=email.provider)]),
    ]
    icons = {d.OK: "OK  ", d.WARN: "WARN", d.FAIL: "FAIL", d.INFO: "info"}
    failed = warned = unknown = 0
    for title, checks in sections:
        print(f"\n  {title}")
        for c in checks:
            failed += c.status == d.FAIL
            warned += c.status == d.WARN
            unknown += c.unknown and title == "Authentication"
            print(f"    [{icons[c.status]}] {c.name:<26} {c.detail}")

    print("\n  " + "=" * 56)
    if failed:
        print(f"  NOT READY - fix the {failed} FAIL item(s) before any cold email.")
    elif unknown:
        print(f"  NOT CHECKED - {unknown} authentication lookup(s) failed; run again.")
    elif warned:
        print(f"  READY WITH CAVEATS - {warned} warning(s); keep volume low.")
    else:
        print("  READY - send within today's cap and watch bounces.")
    print("\n  What DNS can't tell you (the real reputation signal):")
    print("    - Google Postmaster Tools: https://postmaster.google.com - add the domain;")
    print("      it shows Gmail's spam rate and domain reputation once you send volume.")
    print("    - Inbox placement: send one email to a seed test (mail-tester.com is free)")
    print("      and aim for 9/10+.")
    if failed or unknown:
        raise SystemExit(1)


def main():
    from kickstream.textio import utf8_stdio
    utf8_stdio()
    from kickstream.paths import PROJECT_ROOT
    project_root = str(PROJECT_ROOT)

    parser = argparse.ArgumentParser(
        prog="kickstream",
        description="Kickstream — lead gen & outreach for Kick streamers. Make the first move.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # install
    sub = subparsers.add_parser("install", help="Install dependencies")
    sub.set_defaults(func=cmd_install)

    # setup
    sub = subparsers.add_parser("setup", help="Run the interactive setup wizard")
    sub.set_defaults(func=cmd_setup)

    # run
    sub = subparsers.add_parser("run", help="Start Kickstream's heartbeat loop")
    sub.set_defaults(func=cmd_run)

    # dashboard
    sub = subparsers.add_parser("dashboard", help="Open the web dashboard")
    sub.add_argument("--host", default="127.0.0.1",
                     help="Loopback address to listen on (default: 127.0.0.1)")
    sub.add_argument("--port", type=int, default=5555, help="Port (default: 5555)")
    sub.add_argument("--url", action="store_true",
                     help="Print the unlock link (with its token) and exit")
    sub.set_defaults(func=cmd_dashboard)

    # status
    sub = subparsers.add_parser("status", help="Show pipeline status")
    sub.set_defaults(func=cmd_status)

    # discover
    sub = subparsers.add_parser("discover", help="Find Kick streamers via Kick API")
    sub.add_argument("--category", default="", help="Kick category (e.g., 'Just Chatting', 'Gaming')")
    sub.add_argument("--max", type=int, default=100, help="Max streamers to fetch (default: 100)")
    sub.add_argument("--min-followers", type=int, default=500, help="Minimum followers")
    sub.add_argument("--max-followers", type=int, default=50000, help="Maximum followers")
    sub.add_argument("--min-viewers", type=int, default=0, help="Minimum current live viewers")
    sub.add_argument("--max-viewers", type=int, default=0, help="Maximum current live viewers")
    sub.set_defaults(func=cmd_discover)

    # profile
    sub = subparsers.add_parser("profile", help="Scrape streamer profiles for buying signals (free)")
    sub.add_argument("--limit", type=int, default=50, help="Max profiles to scrape (default: 50)")
    sub.add_argument("--stale-days", type=int, default=30, help="Re-scrape after this many days (default: 30)")
    sub.set_defaults(func=cmd_profile)

    # signals
    sub = subparsers.add_parser("signals", help="Review/confirm which signals to prospect against")
    sub.add_argument("--confirm", metavar="CODES", default="",
                     help="Comma-separated codes to confirm ('all' or 'free' accepted)")
    sub.add_argument("--reject", metavar="CODES", default="",
                     help="Comma-separated codes to turn off")
    sub.set_defaults(func=cmd_signals)

    # export
    sub = subparsers.add_parser("export", help="Export contacts as a sequencer-ready CSV")
    sub.add_argument("--out", default="contacts.csv", help="Output file (default: contacts.csv)")
    sub.add_argument("--email-status", default="",
                     help="Comma-separated statuses to include (default: verified,risky)")
    sub.add_argument("--min-score", type=int, default=0, help="Minimum ICP score")
    sub.add_argument("--status", default="", help="Comma-separated pipeline statuses")
    sub.add_argument("--all", action="store_true", help="Export raw list without filters")
    sub.set_defaults(func=cmd_export)

    # outbox
    sub = subparsers.add_parser("outbox", help="Review/approve queued emails")
    sub.add_argument("--approve", metavar="ID", default="", help="Approve one item")
    sub.add_argument("--approve-all", action="store_true", help="Approve all pending")
    sub.add_argument("--reject", metavar="ID", default="", help="Reject one item")
    sub.set_defaults(func=cmd_outbox)

    # sending
    sub = subparsers.add_parser("sending", help="Kill switch: pause/resume sending")
    sub.add_argument("sending_action", nargs="?", default="status",
                     choices=["pause", "resume", "status"])
    sub.set_defaults(func=cmd_sending)

    # warmup
    sub = subparsers.add_parser("warmup", help="Check if sending domain is ready for cold email")
    sub.add_argument("domain", nargs="?", default="",
                     help="Domain to check (default: your sending mailbox's domain)")
    sub.set_defaults(func=cmd_warmup)

    # usage
    sub = subparsers.add_parser("usage", help="Show brain usage and quota (Claude or Codex)")
    sub.add_argument("--days", type=int, default=30, help="Breakdown window (default: 30)")
    sub.set_defaults(func=lambda args: print("Usage command not yet implemented"))

    args = parser.parse_args()
    args._project_root = project_root

    from kickstream import home
    from kickstream.paths import PROJECT_ROOT as home_root
    try:
        home.seed_home(home_root, home.bundled_defaults())
    except Exception as e:
        reason = (e.strerror if isinstance(e, OSError) else None) or type(e).__name__
        print(f"  Could not prepare the home folder {home_root}: {reason}", file=sys.stderr)

    if args.command is None:
        parser.print_help()
        print("\n  Quick start:")
        print("    kickstream install   — Install dependencies")
        print("    kickstream setup     — Configure Kickstream (first time)")
        print("    kickstream run       — Start finding streamers")
        print("    kickstream dashboard — Open the web dashboard")
        print()
        sys.exit(0)

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n  Interrupted. Goodbye.")
        sys.exit(130)
    except Exception as e:
        from kickstream.config import ConfigError
        if isinstance(e, ConfigError):
            print(f"\n  Configuration problem:\n  {e}\n")
        else:
            print(f"\n  Error running 'kickstream {args.command}': {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()