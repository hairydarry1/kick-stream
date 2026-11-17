"""SQLite state manager for Kickstream. All memory lives here.

Schema uses the observation model: every fact is a row, never a column.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import aiosqlite

from kickstream.models.streamer import Streamer
from kickstream.models.contact import Contact
from kickstream.models.campaign import Campaign, EmailStep
from kickstream.models.conversation import Conversation, Message
from kickstream.paths import DB_PATH
from kickstream import storage
from kickstream.storage import BUSY_TIMEOUT_SECONDS

# Email statuses the Writer drafts for
WRITABLE_EMAIL_STATUSES = ("verified", "risky")

# Settings keys
SENDS_HELD_KEY = "sends_held"
MANUAL_SEND_KEY = "manual_send:"
HAND_SENT_REASON = "sent_by_hand"
REVIEW_NEXT_REPLY_KEY = "review_next_reply:"
HELD_FOR_REVIEW = "held for your review: "
COMPANY_STOP_KEY = "company_stop:"
EMAIL_RULES_PROBLEM_KEY = "email_rules_problem"
BRAIN_STATUS_KEY = "brain_status"

# Free mail domains (shared mailbox providers — two people on one aren't colleagues)
FREE_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "aol.com", "icloud.com", "me.com",
    "mac.com", "proton.me", "protonmail.com", "gmx.com", "gmx.de", "web.de",
    "mail.com", "zoho.com", "yandex.com", "yandex.ru", "qq.com", "163.com",
    "comcast.net", "att.net", "verizon.net", "sbcglobal.net", "bellsouth.net",
    "cox.net", "charter.net", "hey.com", "fastmail.com", "pm.me", "rr.com",
    "earthlink.net", "frontier.com", "frontiernet.net", "windstream.net",
    "centurylink.net", "optonline.net", "roadrunner.com", "juno.com",
    "netzero.net", "mindspring.com", "embarqmail.com", "suddenlink.net",
    "yahoo.co.uk", "yahoo.ca", "yahoo.com.au", "hotmail.co.uk", "hotmail.ca",
    "live.co.uk", "live.ca", "btinternet.com", "sky.com", "virginmedia.com",
    "talktalk.net", "ntlworld.com", "shaw.ca", "rogers.com", "sympatico.ca",
    "telus.net", "bigpond.com", "optusnet.com.au", "orange.fr", "free.fr",
    "wanadoo.fr", "laposte.net", "t-online.de", "libero.it", "mail.ru",
    "naver.com", "seznam.cz", "wp.pl", "o2.pl", "tutanota.com", "tuta.io",
})


def _email_domain(alias: str) -> str:
    return f"lower(substr({alias}.email, instr({alias}.email, '@') + 1))"


def _colleagues(p: str, c: str) -> str:
    """SQL: contact c works with contact p (same streamer)."""
    domain = _email_domain(p)
    free = ", ".join(f"'{d}'" for d in sorted(FREE_MAIL_DOMAINS))
    own = f"(SELECT lower(s.domain) FROM streamers s WHERE s.id = {{x}}.streamer_id)"
    return (
        f"{c}.id != {p}.id AND ("
        f"({p}.streamer_id != '' AND {c}.streamer_id = {p}.streamer_id) OR "
        f"(({p}.streamer_id = '' OR {c}.streamer_id = '') "
        f"AND instr({p}.email, '@') > 0 AND {domain} != '' AND {domain} NOT IN ({free}) "
        f"AND {_email_domain(c)} = {domain} "
        f"AND ({p}.streamer_id = '' OR {domain} = {own.format(x=p)}) "
        f"AND ({c}.streamer_id = '' OR {domain} = {own.format(x=c)})))"
    )


def _streamer_answered(p: str) -> str:
    """SQL: someone at contact p's streamer has replied."""
    return (
        f"EXISTS (SELECT 1 FROM contacts c, settings s "
        f"WHERE s.key = '{COMPANY_STOP_KEY}' || c.id AND {_colleagues(p, 'c')})"
    )


AUTOPILOT_OFF_REASON = "waiting for you: autopilot was turned off before it went out"
RECHECK_PREFIX = "Precleared couldn't check this address; trying again after "
MAYBE_SENT_PREFIX = "may have gone out: "
STALE_SENDING_ERROR = (
    MAYBE_SENT_PREFIX + "Kickstream stopped in the middle of sending it; check "
    "your Sent folder before emailing them again"
)
STALE_SENDING_AFTER = timedelta(minutes=15)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


# ── Schema migrations ─────────────────────────────────────────────────────
# Each entry is an idempotent SQL script. Index + 1 = schema version in PRAGMA user_version.

MIGRATIONS: list[str] = [
    # ── v1: base schema ──
    """
    CREATE TABLE IF NOT EXISTS streamers (
        id TEXT PRIMARY KEY,
        kick_username TEXT UNIQUE NOT NULL,
        display_name TEXT DEFAULT '',
        profile_url TEXT DEFAULT '',
        avatar_url TEXT DEFAULT '',
        category TEXT DEFAULT '',
        followers INTEGER DEFAULT 0,
        current_viewers INTEGER DEFAULT 0,
        total_hours_streamed INTEGER DEFAULT 0,
        partner_status TEXT DEFAULT '',
        location TEXT DEFAULT '',
        bio TEXT DEFAULT '',
        external_id TEXT DEFAULT '',
        domain TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS contacts (
        id TEXT PRIMARY KEY,
        streamer_id TEXT DEFAULT '' REFERENCES streamers(id),
        first_name TEXT DEFAULT '',
        last_name TEXT DEFAULT '',
        email TEXT DEFAULT '',
        email_status TEXT DEFAULT 'guess',
        email_source TEXT DEFAULT '',
        twitter_handle TEXT DEFAULT '',
        discord TEXT DEFAULT '',
        contact_form_url TEXT DEFAULT '',
        role TEXT DEFAULT '',
        status TEXT DEFAULT 'new',
        score INTEGER DEFAULT 0,
        personalization_notes TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS campaigns (
        id TEXT PRIMARY KEY,
        name TEXT DEFAULT '',
        channel TEXT DEFAULT 'email',
        sequence_json TEXT DEFAULT '[]',
        contact_ids_json TEXT DEFAULT '[]',
        status TEXT DEFAULT 'draft',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY,
        contact_id TEXT REFERENCES contacts(id),
        campaign_id TEXT DEFAULT '',
        channel TEXT DEFAULT 'email',
        thread_json TEXT DEFAULT '[]',
        intent TEXT DEFAULT '',
        stage TEXT DEFAULT 'initial_outreach',
        status TEXT DEFAULT 'open',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS feedback (
        id TEXT PRIMARY KEY,
        entity_type TEXT DEFAULT '',
        entity_id TEXT DEFAULT '',
        comment TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS actions (
        id TEXT PRIMARY KEY,
        action_type TEXT,
        agent TEXT,
        details_json TEXT DEFAULT '{}',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS usage_log (
        id TEXT PRIMARY KEY,
        date TEXT UNIQUE,
        claude_calls INTEGER DEFAULT 0,
        usage_percent REAL DEFAULT 0.0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS processed_replies (
        reply_id TEXT PRIMARY KEY,
        processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_streamers_kick_username ON streamers(kick_username);
    CREATE INDEX IF NOT EXISTS idx_contacts_streamer_id ON contacts(streamer_id);
    CREATE INDEX IF NOT EXISTS idx_contacts_status ON contacts(status);
    CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
    CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(status);
    CREATE INDEX IF NOT EXISTS idx_conversations_status ON conversations(status);
    CREATE INDEX IF NOT EXISTS idx_feedback_entity ON feedback(entity_type, entity_id);
    CREATE INDEX IF NOT EXISTS idx_usage_date ON usage_log(date);
    """,
    # ── v2: observations & signal codes ──
    """
    CREATE TABLE IF NOT EXISTS streamer_signal_codes (
        code TEXT PRIMARY KEY,
        label TEXT DEFAULT '',
        description TEXT DEFAULT '',
        category TEXT DEFAULT '',
        value_type TEXT DEFAULT 'text',
        collector TEXT DEFAULT '',
        cost_note TEXT DEFAULT '',
        confidence_floor REAL DEFAULT 0.0,
        status TEXT DEFAULT 'proposed',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS streamer_observations (
        id TEXT PRIMARY KEY,
        streamer_id TEXT DEFAULT '',
        contact_id TEXT DEFAULT '',
        signal_code TEXT NOT NULL,
        collector TEXT DEFAULT '',
        value_num REAL,
        value_text TEXT DEFAULT '',
        confidence REAL DEFAULT 1.0,
        evidence_url TEXT DEFAULT '',
        observed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        run_id TEXT DEFAULT ''
    );

    CREATE INDEX IF NOT EXISTS idx_obs_streamer ON streamer_observations(streamer_id);
    CREATE INDEX IF NOT EXISTS idx_obs_signal ON streamer_observations(signal_code);
    CREATE INDEX IF NOT EXISTS idx_obs_streamer_signal
        ON streamer_observations(streamer_id, signal_code, observed_at);
    CREATE INDEX IF NOT EXISTS idx_obs_run ON streamer_observations(run_id);

    -- Trigger: signal_code MUST exist in streamer_signal_codes
    CREATE TRIGGER IF NOT EXISTS trg_obs_signal_known
    BEFORE INSERT ON streamer_observations
    FOR EACH ROW
    WHEN NEW.signal_code NOT IN (SELECT code FROM streamer_signal_codes)
    BEGIN
        SELECT RAISE(ABORT, 'unknown signal_code: not in streamer_signal_codes vocabulary');
    END;

    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        stage TEXT DEFAULT '',
        status TEXT DEFAULT 'running',
        provider TEXT DEFAULT '',
        records INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0.0,
        params_json TEXT DEFAULT '{}',
        error TEXT DEFAULT '',
        started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ended_at TIMESTAMP
    );

    CREATE INDEX IF NOT EXISTS idx_runs_stage_started ON runs(stage, started_at);
    """,
    # ── v3: outbox & settings ──
    """
    CREATE TABLE IF NOT EXISTS outbox (
        id TEXT PRIMARY KEY,
        contact_id TEXT DEFAULT '',
        step INTEGER DEFAULT 1,
        kind TEXT DEFAULT 'sequence',
        to_email TEXT DEFAULT '',
        subject TEXT DEFAULT '',
        body TEXT DEFAULT '',
        status TEXT DEFAULT 'pending_review',
        send_at TIMESTAMP,
        sent_at TIMESTAMP,
        error TEXT DEFAULT '',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_campaign_step
        ON outbox(contact_id, step)
        WHERE contact_id != '' AND kind = 'sequence';
    CREATE INDEX IF NOT EXISTS idx_outbox_status_send_at ON outbox(status, send_at);
    CREATE INDEX IF NOT EXISTS idx_outbox_contact ON outbox(contact_id);

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT DEFAULT '',
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """,
    # ── v4: usage events (per-call token accounting) ──
    """
    CREATE TABLE IF NOT EXISTS usage_events (
        id TEXT PRIMARY KEY,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        agent TEXT DEFAULT '',
        task TEXT DEFAULT '',
        session_id TEXT DEFAULT '',
        request_key TEXT DEFAULT '',
        model TEXT DEFAULT '',
        input_tokens INTEGER DEFAULT 0,
        output_tokens INTEGER DEFAULT 0,
        cache_read_tokens INTEGER DEFAULT 0,
        cache_creation_tokens INTEGER DEFAULT 0,
        cost_usd REAL DEFAULT 0.0,
        duration_ms INTEGER DEFAULT 0,
        num_turns INTEGER DEFAULT 0,
        is_error INTEGER DEFAULT 0,
        source TEXT DEFAULT 'result_json',
        provider TEXT DEFAULT 'claude'
    );

    CREATE INDEX IF NOT EXISTS idx_usage_events_created ON usage_events(created_at);
    CREATE INDEX IF NOT EXISTS idx_usage_events_agent ON usage_events(agent);
    CREATE INDEX IF NOT EXISTS idx_usage_events_session ON usage_events(session_id);
    CREATE UNIQUE INDEX IF NOT EXISTS uq_usage_events_request
        ON usage_events(request_key) WHERE request_key != '';
    CREATE INDEX IF NOT EXISTS idx_usage_events_provider ON usage_events(provider);
    """,
    # ── v5: normalize emails for uniqueness ──
    """
    UPDATE contacts SET email = LOWER(TRIM(email)) WHERE email != LOWER(TRIM(email));
    UPDATE streamers SET domain = LOWER(TRIM(domain)) WHERE domain != LOWER(TRIM(domain));

    DELETE FROM contacts WHERE email != '' AND rowid NOT IN (
        SELECT MIN(rowid) FROM contacts WHERE email != '' GROUP BY email
    );
    DELETE FROM streamers WHERE domain != '' AND rowid NOT IN (
        SELECT MIN(rowid) FROM streamers WHERE domain != '' GROUP BY domain
    );

    CREATE UNIQUE INDEX IF NOT EXISTS uq_contacts_email
        ON contacts(email) WHERE email != '';
    CREATE UNIQUE INDEX IF NOT EXISTS uq_streamers_domain
        ON streamers(domain) WHERE domain != '';
    CREATE UNIQUE INDEX IF NOT EXISTS uq_streamers_external
        ON streamers(external_id) WHERE external_id != '';
    """,
]


# Column whitelists for dynamic UPDATEs
_STREAMER_COLUMNS = frozenset({
    "kick_username", "display_name", "profile_url", "avatar_url",
    "category", "followers", "current_viewers", "total_hours_streamed",
    "partner_status", "location", "bio", "external_id", "domain",
    "updated_at",
})
_CONTACT_COLUMNS = frozenset({
    "streamer_id", "first_name", "last_name", "email", "email_status",
    "email_source", "twitter_handle", "discord", "contact_form_url",
    "role", "status", "score", "personalization_notes", "updated_at",
})
_CAMPAIGN_COLUMNS = frozenset({
    "name", "channel", "sequence_json", "contact_ids_json", "status",
})
_CONVERSATION_COLUMNS = frozenset({
    "contact_id", "campaign_id", "channel",
    "thread_json", "intent", "stage", "status",
})


class StateManager:
    def __init__(self, db_path: str | None = None):
        if not db_path:
            from kickstream.workspace_home import _HOME
            home = _HOME.get()
            db_path = str(home.db_path) if home is not None else str(DB_PATH)
        self.db_path = db_path

    @asynccontextmanager
    async def _connect(self):
        async with storage.connect(self.db_path, timeout=BUSY_TIMEOUT_SECONDS) as db:
            yield db

    async def init_db(self):
        """Create/upgrade the schema. Safe to call on every startup."""
        if storage.manages_schema(self.db_path):
            await storage.check_schema(self.db_path)
            return
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")

            async with db.execute("PRAGMA user_version") as cursor:
                (version,) = await cursor.fetchone()

            for target, script in enumerate(MIGRATIONS, start=1):
                if version < target:
                    await db.executescript(script)
                    await db.execute(f"PRAGMA user_version = {target}")
                    await db.commit()

    # ── Streamers ──

    @staticmethod
    def _streamer_from_row(row: aiosqlite.Row) -> Streamer:
        return Streamer(**dict(row))

    async def add_streamer(self, streamer: Streamer) -> str:
        """Insert a streamer, or return the id of the one already recorded."""
        if not streamer.id:
            streamer.id = _new_id()
        streamer.kick_username = _norm(streamer.kick_username)
        streamer.external_id = streamer.external_id or f"kick:{streamer.kick_username}"
        streamer.domain = _norm(streamer.domain)
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO streamers
                   (id, kick_username, display_name, profile_url, avatar_url,
                    category, followers, current_viewers, total_hours_streamed,
                    partner_status, location, bio, external_id, domain,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    streamer.id, streamer.kick_username, streamer.display_name,
                    streamer.profile_url, streamer.avatar_url, streamer.category,
                    streamer.followers, streamer.current_viewers, streamer.total_hours_streamed,
                    streamer.partner_status, streamer.location, streamer.bio,
                    streamer.external_id, streamer.domain,
                    streamer.created_at.isoformat(), streamer.updated_at.isoformat(),
                ),
            )
            await db.commit()
            if cursor.rowcount == 0:
                # Uniqueness conflict: hand back the existing record's id
                for column, value in (("kick_username", streamer.kick_username),
                                      ("external_id", streamer.external_id),
                                      ("domain", streamer.domain)):
                    if not value:
                        continue
                    async with db.execute(
                        f"SELECT id FROM streamers WHERE {column} = ?", (value,)
                    ) as cur:
                        row = await cur.fetchone()
                        if row:
                            streamer.id = row[0]
                            break
        return streamer.id

    async def get_streamer(self, streamer_id: str) -> Streamer | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM streamers WHERE id = ?", (streamer_id,)) as cur:
                row = await cur.fetchone()
                return self._streamer_from_row(row) if row else None

    async def get_streamer_by_kick_username(self, username: str) -> Streamer | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM streamers WHERE kick_username = ?", (_norm(username),)) as cur:
                row = await cur.fetchone()
                return self._streamer_from_row(row) if row else None

    async def update_streamer(self, streamer_id: str, **kwargs):
        """Update streamer fields (whitelisted)."""
        updates = {k: v for k, v in kwargs.items() if k in _STREAMER_COLUMNS}
        if not updates:
            return
        updates["updated_at"] = _utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE streamers SET {set_clause} WHERE id = ?",
                (*updates.values(), streamer_id),
            )
            await db.commit()

    async def list_streamers(self, limit: int = 100, offset: int = 0, status: str | None = None) -> list[Streamer]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            sql = "SELECT * FROM streamers"
            params: list = []
            if status:
                sql += " WHERE status = ?"
                params.append(status)
            sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
                return [self._streamer_from_row(r) for r in rows]

    # ── Contacts ──

    @staticmethod
    def _contact_from_row(row: aiosqlite.Row) -> Contact:
        return Contact(**dict(row))

    async def add_contact(self, contact: Contact) -> str:
        """Insert a contact, or return the id of the one already recorded."""
        if not contact.id:
            contact.id = _new_id()
        contact.email = _norm(contact.email)
        async with self._connect() as db:
            cursor = await db.execute(
                """INSERT OR IGNORE INTO contacts
                   (id, streamer_id, first_name, last_name, email, email_status,
                    email_source, twitter_handle, discord, contact_form_url,
                    role, status, score, personalization_notes,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    contact.id, contact.streamer_id, contact.first_name, contact.last_name,
                    contact.email, contact.email_status, contact.email_source,
                    contact.twitter_handle, contact.discord, contact.contact_form_url,
                    contact.role, contact.status, contact.score, contact.personalization_notes,
                    contact.created_at.isoformat(), contact.updated_at.isoformat(),
                ),
            )
            await db.commit()
            if cursor.rowcount == 0:
                for column, value in (("email", contact.email),):
                    if not value:
                        continue
                    async with db.execute(
                        f"SELECT id FROM contacts WHERE {column} = ?", (value,)
                    ) as cur:
                        row = await cur.fetchone()
                        if row:
                            contact.id = row[0]
                            break
        return contact.id

    async def get_contact(self, contact_id: str) -> Contact | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)) as cur:
                row = await cur.fetchone()
                return self._contact_from_row(row) if row else None

    async def get_contacts_by_streamer(self, streamer_id: str) -> list[Contact]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM contacts WHERE streamer_id = ?", (streamer_id,)) as cur:
                rows = await cur.fetchall()
                return [self._contact_from_row(r) for r in rows]

    async def update_contact(self, contact_id: str, **kwargs):
        updates = {k: v for k, v in kwargs.items() if k in _CONTACT_COLUMNS}
        if not updates:
            return
        updates["updated_at"] = _utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE contacts SET {set_clause} WHERE id = ?",
                (*updates.values(), contact_id),
            )
            await db.commit()

    async def get_prospects_by_status(self, status: str) -> list[Contact]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM contacts WHERE status = ? ORDER BY created_at", (status,)) as cur:
                rows = await cur.fetchall()
                return [self._contact_from_row(r) for r in rows]

    # ── Signal Codes ──

    async def seed_signal_catalog(self, signals: list[dict]):
        """Insert or update signal codes from a catalog definition."""
        async with self._connect() as db:
            for s in signals:
                await db.execute(
                    """INSERT OR REPLACE INTO streamer_signal_codes
                       (code, label, description, category, value_type, collector,
                        cost_note, confidence_floor, status, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        s["code"], s.get("label", ""), s.get("description", ""),
                        s.get("category", ""), s.get("value_type", "text"),
                        s.get("collector", ""), s.get("cost_note", ""),
                        s.get("confidence_floor", 0.0), s.get("status", "proposed"),
                        _utcnow().isoformat(),
                    ),
                )
            await db.commit()

    async def get_signal_codes(self) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM streamer_signal_codes ORDER BY category, code") as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def set_signal_status(self, code: str, status: str) -> bool:
        """Set signal status (proposed/confirmed/rejected). Returns True if changed."""
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE streamer_signal_codes SET status = ?, updated_at = ? WHERE code = ? AND status != ?",
                (status, _utcnow().isoformat(), code.upper(), status),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def signal_counts(self) -> list[dict]:
        """Count streamers per signal code."""
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("""
                SELECT o.signal_code, COUNT(DISTINCT o.streamer_id) as companies
                FROM streamer_observations o
                JOIN streamer_signal_codes s ON s.code = o.signal_code
                WHERE s.status = 'confirmed'
                GROUP BY o.signal_code
            """) as cur:
                return [dict(r) for r in await cur.fetchall()]

    # ── Observations ──

    async def add_observation(self, obs: dict) -> str:
        """Insert an observation. signal_code must exist in streamer_signal_codes."""
        obs_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO streamer_observations
                   (id, streamer_id, contact_id, signal_code, collector,
                    value_num, value_text, confidence, evidence_url, observed_at, run_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    obs_id, obs.get("streamer_id", ""), obs.get("contact_id", ""),
                    obs["signal_code"], obs.get("collector", ""),
                    obs.get("value_num"), obs.get("value_text", ""),
                    obs.get("confidence", 1.0), obs.get("evidence_url", ""),
                    obs.get("observed_at", _utcnow().isoformat()), obs.get("run_id", ""),
                ),
            )
            await db.commit()
        return obs_id

    async def get_observations(self, streamer_id: str, signal_code: str | None = None) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if signal_code:
                async with db.execute(
                    "SELECT * FROM streamer_observations WHERE streamer_id = ? AND signal_code = ? ORDER BY observed_at DESC",
                    (streamer_id, signal_code),
                ) as cur:
                    return [dict(r) for r in await cur.fetchall()]
            else:
                async with db.execute(
                    "SELECT * FROM streamer_observations WHERE streamer_id = ? ORDER BY observed_at DESC",
                    (streamer_id,),
                ) as cur:
                    return [dict(r) for r in await cur.fetchall()]

    # ── Outbox ──

    async def add_outbox_item(self, item: dict) -> str:
        item_id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO outbox
                   (id, contact_id, step, kind, to_email, subject, body,
                    status, send_at, error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_id, item.get("contact_id", ""), item.get("step", 1),
                    item.get("kind", "sequence"), item["to_email"], item["subject"],
                    item["body"], item.get("status", "pending_review"),
                    item.get("send_at"), item.get("error", ""),
                    _utcnow().isoformat(), _utcnow().isoformat(),
                ),
            )
            await db.commit()
        return item_id

    async def get_outbox(self, status: str | None = None, limit: int = 50) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            if status:
                async with db.execute(
                    "SELECT * FROM outbox WHERE status = ? ORDER BY send_at LIMIT ?", (status, limit)
                ) as cur:
                    return [dict(r) for r in await cur.fetchall()]
            else:
                async with db.execute(
                    "SELECT * FROM outbox ORDER BY send_at LIMIT ?", (limit,)
                ) as cur:
                    return [dict(r) for r in await cur.fetchall()]

    async def approve_outbox(self, item_id: str | None = None) -> int:
        async with self._connect() as db:
            if item_id:
                cursor = await db.execute(
                    "UPDATE outbox SET status = 'approved', updated_at = ? WHERE id = ? AND status = 'pending_review'",
                    (_utcnow().isoformat(), item_id),
                )
            else:
                cursor = await db.execute(
                    "UPDATE outbox SET status = 'approved', updated_at = ? WHERE status = 'pending_review'",
                    (_utcnow().isoformat(),),
                )
            await db.commit()
            return cursor.rowcount

    async def reject_outbox_item(self, item_id: str) -> int:
        async with self._connect() as db:
            cursor = await db.execute(
                "UPDATE outbox SET status = 'rejected', updated_at = ? WHERE id = ? AND status = 'pending_review'",
                (_utcnow().isoformat(), item_id),
            )
            await db.commit()
            return cursor.rowcount

    async def count_held_for_review(self) -> int:
        async with self._connect() as db:
            async with db.execute("SELECT COUNT(*) FROM outbox WHERE status = 'pending_review'") as cur:
                (count,) = await cur.fetchone()
                return count or 0

    # ── Settings KV ──

    async def get_setting(self, key: str) -> str | None:
        async with self._connect() as db:
            async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
                row = await cur.fetchone()
                return row[0] if row else None

    async def set_setting(self, key: str, value: str):
        async with self._connect() as db:
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, _utcnow().isoformat()),
            )
            await db.commit()

    async def delete_setting(self, key: str):
        async with self._connect() as db:
            await db.execute("DELETE FROM settings WHERE key = ?", (key,))
            await db.commit()

    async def note_sends_held(self, reason: str):
        import json
        await self.set_setting(SENDS_HELD_KEY, json.dumps({
            "since": _utcnow().isoformat(),
            "last_seen": _utcnow().isoformat(),
            "reason": reason,
        }))

    async def clear_sends_held(self):
        await self.delete_setting(SENDS_HELD_KEY)

    async def current_sends_held(self) -> dict | None:
        raw = await self.get_setting(SENDS_HELD_KEY)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    # ── Actions / Usage ──

    async def log_action(self, action_type: str, agent: str, details: dict | None = None):
        async with self._connect() as db:
            await db.execute(
                "INSERT INTO actions (id, action_type, agent, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (_new_id(), action_type, agent, json.dumps(details or {}), _utcnow().isoformat()),
            )
            await db.commit()

    async def get_state_summary(self) -> dict:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            summary = {}

            # Streamers
            async with db.execute("SELECT COUNT(*) FROM streamers") as cur:
                summary["streamers"] = (await cur.fetchone())[0]

            # Contacts by status
            async with db.execute("""
                SELECT status, COUNT(*) FROM contacts GROUP BY status
            """) as cur:
                prospects = {row[0]: row[1] for row in await cur.fetchall()}
                summary["prospects"] = prospects
                summary["writable_prospects"] = sum(
                    v for k, v in prospects.items() if k in ("new", "queued")
                )

            # Campaigns
            async with db.execute("SELECT status, COUNT(*) FROM campaigns GROUP BY status") as cur:
                summary["campaigns"] = {row[0]: row[1] for row in await cur.fetchall()}
                summary["draft_campaigns"] = summary["campaigns"].get("draft", 0)
                summary["active_campaigns"] = summary["campaigns"].get("active", 0)

            # Conversations
            async with db.execute("SELECT status, COUNT(*) FROM conversations GROUP BY status") as cur:
                summary["conversations"] = {row[0]: row[1] for row in await cur.fetchall()}
                summary["open_conversations"] = summary["conversations"].get("open", 0)

            # Usage today
            today = date.today().isoformat()
            async with db.execute("SELECT claude_calls FROM usage_log WHERE date = ?", (today,)) as cur:
                row = await cur.fetchone()
                summary["usage_today"] = row[0] if row else 0

            # Sending paused
            paused = await self.get_setting("sending_paused")
            summary["sending_paused"] = paused

            # Unusable prospects (guess/invalid emails found recently)
            async with db.execute("""
                SELECT COUNT(*) FROM contacts
                WHERE email_status IN ('guess', 'invalid') AND created_at > datetime('now', '-1 day')
            """) as cur:
                summary["recent_unusable_prospects"] = (await cur.fetchone())[0]

            # Unprofiled streamers
            async with db.execute("""
                SELECT COUNT(*) FROM streamers s
                WHERE NOT EXISTS (
                    SELECT 1 FROM streamer_observations o WHERE o.streamer_id = s.id AND o.collector = 'profile'
                )
            """) as cur:
                summary["unprofiled"] = (await cur.fetchone())[0]

            return summary

    # ── Usage events ──

    async def record_usage(self, event: dict):
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO usage_events
                   (id, agent, task, session_id, request_key, model,
                    input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens,
                    cost_usd, duration_ms, num_turns, is_error, source, provider, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _new_id(), event.get("agent", ""), event.get("task", ""),
                    event.get("session_id", ""), event.get("request_key", ""),
                    event.get("model", ""), event.get("input_tokens", 0),
                    event.get("output_tokens", 0), event.get("cache_read_tokens", 0),
                    event.get("cache_creation_tokens", 0), event.get("cost_usd", 0.0),
                    event.get("duration_ms", 0), event.get("num_turns", 0),
                    event.get("is_error", 0), event.get("source", "result_json"),
                    event.get("provider", "claude"), _utcnow().isoformat(),
                ),
            )
            await db.commit()

    async def usage_totals(self) -> dict:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            totals = {}
            for label, days in (("today", 0), ("week", 7), ("month", 30)):
                since = (date.today() - timedelta(days=days)).isoformat() if days else date.today().isoformat()
                async with db.execute("""
                    SELECT COUNT(*) as calls, SUM(input_tokens) as input_tokens,
                           SUM(output_tokens) as output_tokens, SUM(cache_read_tokens) as cache_read_tokens
                    FROM usage_events WHERE date(created_at) >= ?
                """, (since,)) as cur:
                    row = await cur.fetchone()
                    totals[label] = dict(row) if row else {}
            return totals

    async def usage_by_provider(self, days: int = 30) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            since = (date.today() - timedelta(days=days)).isoformat()
            async with db.execute("""
                SELECT provider, COUNT(*) as calls, SUM(output_tokens) as output_tokens
                FROM usage_events WHERE date(created_at) >= ? GROUP BY provider
            """, (since,)) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def usage_by_agent(self, days: int = 30) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            since = (date.today() - timedelta(days=days)).isoformat()
            async with db.execute("""
                SELECT agent, COUNT(*) as calls, SUM(output_tokens) as output_tokens
                FROM usage_events WHERE date(created_at) >= ? GROUP BY agent
            """, (since,)) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def usage_by_task(self, days: int = 30) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            since = (date.today() - timedelta(days=days)).isoformat()
            async with db.execute("""
                SELECT task, COUNT(*) as calls, SUM(output_tokens) as output_tokens
                FROM usage_events WHERE date(created_at) >= ? GROUP BY task
            """, (since,)) as cur:
                return [dict(r) for r in await cur.fetchall()]

    # ── Cohort queries ──

    async def cohort(self, require: list[str], exclude: list[str]) -> list[str]:
        """Return streamer_ids matching ALL required signals and NONE of excluded."""
        if not require:
            return []
        placeholders = ", ".join("?" for _ in require)
        async with self._connect() as db:
            # Start with streamers having the first required signal
            sql = f"""
                SELECT DISTINCT o1.streamer_id FROM streamer_observations o1
                JOIN streamer_signal_codes s1 ON s1.code = o1.signal_code
                WHERE s1.status = 'confirmed' AND o1.signal_code = ?
            """
            params = [require[0]]
            # Intersect with remaining required signals
            for code in require[1:]:
                sql += f"""
                    AND o1.streamer_id IN (
                        SELECT streamer_id FROM streamer_observations o
                        JOIN streamer_signal_codes s ON s.code = o.signal_code
                        WHERE s.status = 'confirmed' AND o.signal_code = ?
                    )
                """
                params.append(code)
            # Exclude excluded signals
            for code in exclude:
                sql += f"""
                    AND o1.streamer_id NOT IN (
                        SELECT streamer_id FROM streamer_observations o
                        JOIN streamer_signal_codes s ON s.code = o.signal_code
                        WHERE s.status = 'confirmed' AND o.signal_code = ?
                    )
                """
                params.append(code)
            async with db.execute(sql, params) as cur:
                return [row[0] for row in await cur.fetchall()]

    # ── Suppressions ──

    def suppression_key(self, kind: str, value: str) -> str:
        return f"{kind}:{_norm(value)}"

    async def add_suppression(self, kind: str, value: str, reason: str = "manual", source: str = "cli") -> bool:
        key = self.suppression_key(kind, value)
        async with self._connect() as db:
            cursor = await db.execute(
                "INSERT OR IGNORE INTO suppressions (id, kind, value, reason, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (key, kind, _norm(value), reason, source, _utcnow().isoformat()),
            )
            await db.commit()
            return cursor.rowcount > 0

    async def remove_suppression(self, key: str) -> bool:
        async with self._connect() as db:
            cursor = await db.execute("DELETE FROM suppressions WHERE id = ?", (key,))
            await db.commit()
            return cursor.rowcount > 0

    async def list_suppressions(self, limit: int = 1000) -> list[dict]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM suppressions ORDER BY created_at DESC LIMIT ?", (limit,)) as cur:
                return [dict(r) for r in await cur.fetchall()]

    async def is_suppressed(self, kind: str, value: str) -> bool:
        key = self.suppression_key(kind, value)
        async with self._connect() as db:
            async with db.execute("SELECT 1 FROM suppressions WHERE id = ?", (key,)) as cur:
                return await cur.fetchone() is not None

    # ── Email verification ──

    async def get_email_pattern(self, domain: str) -> dict | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM email_patterns WHERE domain = ?", (_norm(domain),)) as cur:
                row = await cur.fetchone()
                return dict(row) if row else None

    async def set_email_pattern(self, domain: str, pattern: str, source: str, confidence: float, mx_type: str, is_catch_all: int):
        async with self._connect() as db:
            await db.execute(
                """INSERT OR REPLACE INTO email_patterns
                   (domain, pattern, source, confidence, mx_type, is_catch_all, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (_norm(domain), pattern, source, confidence, mx_type, is_catch_all, _utcnow().isoformat()),
            )
            await db.commit()

    # ── Campaigns ──

    async def add_campaign(self, campaign: Campaign) -> str:
        if not campaign.id:
            campaign.id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO campaigns
                   (id, name, channel, sequence_json, contact_ids_json, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    campaign.id, campaign.name, campaign.channel,
                    json.dumps([s.model_dump() for s in campaign.sequence]),
                    json.dumps(campaign.contact_ids), campaign.status,
                    campaign.created_at.isoformat(),
                ),
            )
            await db.commit()
        return campaign.id

    async def get_campaign(self, campaign_id: str) -> Campaign | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                d = dict(row)
                d["sequence"] = json.loads(d.pop("sequence_json", "[]"))
                d["contact_ids"] = json.loads(d.pop("contact_ids_json", "[]"))
                return Campaign(**d)

    async def update_campaign(self, campaign_id: str, **kwargs):
        updates = {k: v for k, v in kwargs.items() if k in _CAMPAIGN_COLUMNS}
        if not updates:
            return
        if "sequence" in updates:
            updates["sequence_json"] = json.dumps([s.model_dump() for s in updates.pop("sequence")])
        if "contact_ids" in updates:
            updates["contact_ids_json"] = json.dumps(updates.pop("contact_ids"))
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE campaigns SET {set_clause} WHERE id = ?",
                (*updates.values(), campaign_id),
            )
            await db.commit()

    # ── Conversations ──

    async def add_conversation(self, conversation: Conversation) -> str:
        if not conversation.id:
            conversation.id = _new_id()
        async with self._connect() as db:
            await db.execute(
                """INSERT INTO conversations
                   (id, contact_id, campaign_id, channel, thread_json, intent, stage, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    conversation.id, conversation.contact_id, conversation.campaign_id,
                    conversation.channel, json.dumps([m.model_dump() for m in conversation.thread]),
                    conversation.intent, conversation.stage, conversation.status,
                    conversation.created_at.isoformat(), conversation.updated_at.isoformat(),
                ),
            )
            await db.commit()
        return conversation.id

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                d = dict(row)
                d["thread"] = json.loads(d.pop("thread_json", "[]"))
                return Conversation(**d)

    async def get_conversations_by_contact(self, contact_id: str) -> list[Conversation]:
        async with self._connect() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM conversations WHERE contact_id = ?", (contact_id,)) as cur:
                rows = await cur.fetchall()
                result = []
                for row in rows:
                    d = dict(row)
                    d["thread"] = json.loads(d.pop("thread_json", "[]"))
                    result.append(Conversation(**d))
                return result

    async def update_conversation(self, conversation_id: str, **kwargs):
        updates = {k: v for k, v in kwargs.items() if k in _CONVERSATION_COLUMNS}
        if not updates:
            return
        if "thread" in updates:
            updates["thread_json"] = json.dumps([m.model_dump() for m in updates.pop("thread")])
        updates["updated_at"] = _utcnow().isoformat()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        async with self._connect() as db:
            await db.execute(
                f"UPDATE conversations SET {set_clause} WHERE id = ?",
                (*updates.values(), conversation_id),
            )
            await db.commit()

    async def colleague_who_replied(self, contact_id: str) -> bool:
        """True if someone at this contact's streamer has replied."""
        async with self._connect() as db:
            async with db.execute(
                f"SELECT 1 FROM contacts c, settings s WHERE s.key = '{COMPANY_STOP_KEY}' || c.id AND {_colleagues('c', 'p')} LIMIT 1",
                (contact_id,),
            ) as cur:
                return await cur.fetchone() is not None

    async def mark_prospects_checked(self, streamer_id: str):
        """Mark a streamer as having been checked for contacts (backfill rotation)."""
        await self.update_streamer(streamer_id, prospects_checked_at=_utcnow().isoformat())