"""Postgres-backed conversation memory + user registry.

Two tables:

  users
    user_id          BIGINT PRIMARY KEY  -- Telegram user id
    username, first_name, last_name, language_code, is_bot
    joined_at, last_active_at  TIMESTAMPTZ
    query_count                INTEGER

  conversations
    (chat_id, user_id, turn_idx)         -- composite uniqueness
    role, content, ts

The bot upserts a `users` row on every incoming message and inserts a
pair of `conversations` rows after each successful answer. The `/reset`
command deletes a user's conversation rows but keeps their user record.

Connection: read `DATABASE_URL` from the environment. Default points at
the docker-compose `postgres` service published on localhost:5433
(5433, not 5432, to avoid clashing with any host-installed Postgres).
"""

from __future__ import annotations

import os
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterable

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://ensia:ensia@localhost:5433/ensia_bot",
)

# Redact anything that looks like a credential before persisting a log
# line to the events table. Tracebacks and error strings can embed the
# DATABASE_URL, API keys, or bearer tokens; the events table is readable
# from the dashboard, so scrub first.
_SECRET_PATTERNS = [
    re.compile(r"\b[a-z][a-z0-9+.\-]*://[^\s/@]+:[^\s/@]+@", re.I),  # user:pass@ in any URL
    re.compile(r"\b(sk|hf|gsk|xoxb|ghp|glpat)[-_][A-Za-z0-9\-_]{8,}"),  # common key prefixes
    re.compile(r"(?i)\b(authorization|api[_-]?key|token|password|secret)\b\s*[:=]\s*\S+"),
]


def _scrub_secrets(text: str | None) -> str | None:
    """Best-effort redaction of credentials embedded in a log message or
    traceback before it lands in the (dashboard-readable) events table."""
    if not text:
        return text
    out = text
    out = _SECRET_PATTERNS[0].sub(lambda m: m.group(0).split("://")[0] + "://***:***@", out)
    out = _SECRET_PATTERNS[1].sub("***", out)
    out = _SECRET_PATTERNS[2].sub(r"\1=***", out)
    return out

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id          BIGINT PRIMARY KEY,
    username         TEXT,
    first_name       TEXT,
    last_name        TEXT,
    language_code    TEXT,
    is_bot           BOOLEAN NOT NULL DEFAULT FALSE,
    joined_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_active_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    query_count      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS conversations (
    id          BIGSERIAL PRIMARY KEY,
    chat_id     BIGINT NOT NULL,
    user_id     BIGINT NOT NULL,
    turn_idx    INTEGER NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (chat_id, user_id, turn_idx)
);

CREATE INDEX IF NOT EXISTS idx_conv_recent
    ON conversations (chat_id, user_id, turn_idx DESC);

-- Per-turn citations payload. NULL for user turns and for assistant turns
-- with no sources (small talk, refusals). Shape mirrors
-- chatbot.answer._format_sources output (id, number, score, metadata, preview).
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS sources JSONB;

-- When a user calls /reset we mark their prior turns with a timestamp
-- instead of deleting them, so the LLM's history queries skip past
-- turns while the dashboard still shows the full audit trail.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS cleared_at TIMESTAMPTZ;

-- Telegram message_id of the bot's outgoing reply. Lets us link inline-
-- keyboard feedback callbacks back to the assistant turn that produced
-- them. NULL for user turns and for any historical row that predates
-- this column.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS tg_message_id BIGINT;

-- Which LLM actually answered this turn. NULL on user turns and on
-- assistant turns from before this column existed. Useful for spotting
-- in the dashboard when the bot fell back from the primary model to a
-- secondary one (Groq instead of Gemini, etc.).
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS model_used TEXT;

-- Whether the user invoked /deep on this turn. Lets us award the
-- "Deep Diver" gamification badge without scanning text.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS rerank BOOLEAN NOT NULL DEFAULT FALSE;

-- Whether this exchange was refused (off-topic / out-of-scope / small
-- talk the bot declined to ground). Marked on BOTH the user turn and
-- the assistant turn. The dashboard surfaces these (so query_count
-- always has a matching transcript) but recent_turns() skips them so
-- a refused exchange never pollutes the LLM's follow-up context.
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS refused BOOLEAN NOT NULL DEFAULT FALSE;

-- Per-answer telemetry (set on the assistant turn). Previously only in
-- the ephemeral HF logs; persisted here so latency / retrieval-quality
-- are queryable across restarts (p95 latency, slowest queries, tier mix).
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS latency_ms INTEGER;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS tier TEXT;
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS top_score REAL;

-- ── Gamification ──────────────────────────────────────────────────
-- Personal stats: streak counters + earned-badge keys. Computed lazily
-- on /me and refreshed on every successful answer. Don't reset on the
-- user's /reset — those are about behaviour over time, not the current
-- conversation memory.
ALTER TABLE users ADD COLUMN IF NOT EXISTS current_streak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS best_streak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_streak_date DATE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS badges JSONB NOT NULL DEFAULT '[]'::jsonb;

CREATE TABLE IF NOT EXISTS feedback (
    id          BIGSERIAL PRIMARY KEY,
    chat_id     BIGINT NOT NULL,
    message_id  BIGINT NOT NULL,   -- bot's reply tg msg id
    user_id     BIGINT NOT NULL,
    rating      TEXT NOT NULL CHECK (rating IN ('useful','not_useful','report')),
    reason      TEXT,              -- only populated for 'report'
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (chat_id, message_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_msg
    ON feedback (chat_id, message_id);

-- Structured operational log. A logging.Handler mirrors every WARNING+
-- record here (crashes, rate-limits, failed edits, etc.) so failures
-- survive the Space's frequent restarts — HF stdout logs are ephemeral
-- and rotate within hours. NOT a raw stdout tee: INFO chatter (litellm,
-- uvicorn) is deliberately excluded to keep this table small and useful.
CREATE TABLE IF NOT EXISTS events (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    level       TEXT NOT NULL,        -- WARNING / ERROR / CRITICAL
    logger      TEXT,                 -- e.g. ensia.bot.telethon
    message     TEXT NOT NULL,        -- formatted log message (secrets scrubbed)
    exc_info    TEXT                  -- traceback, if the record had one
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts DESC);
"""


class ConversationMemory:
    """Synchronous Postgres client. Cheap to instantiate; opens a fresh
    connection per call. For our workload (a few requests per second
    peak) that's plenty — if it ever isn't, swap to psycopg_pool."""

    def __init__(self, dsn: str = DEFAULT_DSN):
        self.dsn = dsn
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(_SCHEMA)

    @contextmanager
    def _conn(self):
        with psycopg.connect(self.dsn, row_factory=dict_row) as conn:
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ─── users ──────────────────────────────────────────────────────────

    def upsert_user(
        self,
        user_id: int,
        *,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        language_code: str | None = None,
        is_bot: bool = False,
    ) -> None:
        """Record / refresh a user. Safe to call on every message —
        existing fields are preserved when the new value is None, so a
        Telegram update missing optional fields doesn't blank anything."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (
                        user_id, username, first_name, last_name,
                        language_code, is_bot, last_active_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        username = COALESCE(EXCLUDED.username, users.username),
                        first_name = COALESCE(EXCLUDED.first_name, users.first_name),
                        last_name = COALESCE(EXCLUDED.last_name, users.last_name),
                        language_code = COALESCE(EXCLUDED.language_code, users.language_code),
                        is_bot = EXCLUDED.is_bot,
                        last_active_at = NOW()
                    """,
                    (user_id, username, first_name, last_name,
                     language_code, is_bot),
                )

    def increment_query_count(self, user_id: int) -> None:
        """Bump a user's query counter. Caller decides what counts as a
        query — currently called once per accepted (non-rate-limited)
        message in the bot."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET query_count = query_count + 1, "
                    "last_active_at = NOW() WHERE user_id = %s",
                    (user_id,),
                )

    # ─── conversation memory ────────────────────────────────────────────

    def add_turns(
        self,
        chat_id: int,
        user_id: int,
        turns: Iterable[dict],
    ) -> None:
        """Append a sequence of {role, content} dicts atomically. The
        next turn_idx is derived from MAX(turn_idx)+1 within the same
        (chat, user) so concurrent inserts from the same user are safe
        under Postgres' default isolation."""
        turns = list(turns)
        if not turns:
            return
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COALESCE(MAX(turn_idx), -1) AS maxi "
                    "FROM conversations WHERE chat_id=%s AND user_id=%s",
                    (chat_id, user_id),
                )
                next_idx = cur.fetchone()["maxi"] + 1
                from psycopg.types.json import Jsonb
                rows = [
                    (
                        chat_id,
                        user_id,
                        next_idx + i,
                        t["role"],
                        t["content"],
                        Jsonb(t["sources"]) if t.get("sources") else None,
                        t.get("tg_message_id"),
                        t.get("model_used"),
                        bool(t.get("rerank")),
                        bool(t.get("refused")),
                        t.get("latency_ms"),
                        t.get("tier"),
                        t.get("top_score"),
                    )
                    for i, t in enumerate(turns)
                ]
                cur.executemany(
                    "INSERT INTO conversations "
                    "(chat_id, user_id, turn_idx, role, content, sources, tg_message_id, "
                    "model_used, rerank, refused, latency_ms, tier, top_score) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    rows,
                )

    def recent_turns(
        self,
        chat_id: int,
        user_id: int,
        n: int = 5,
        max_age_hours: float = 24.0,
    ) -> list[dict]:
        """Return the last `n` exchanges (≤ 2n rows) within max_age_hours,
        ordered oldest → newest. Each row is `{role, content}`."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT role, content
                    FROM conversations
                    WHERE chat_id=%s AND user_id=%s AND ts >= %s
                      AND cleared_at IS NULL
                      AND refused IS NOT TRUE
                    ORDER BY turn_idx DESC
                    LIMIT %s
                    """,
                    (chat_id, user_id, cutoff, n * 2),
                )
                rows = cur.fetchall()
        return list(reversed(
            [{"role": r["role"], "content": r["content"]} for r in rows]
        ))

    # ─── feedback (inline-keyboard ratings) ─────────────────────────────

    def set_feedback(
        self,
        chat_id: int,
        message_id: int,
        user_id: int,
        rating: str,
    ) -> str:
        """Record (or toggle) a feedback click. Re-tapping the same rating
        a user already gave for that bot reply *removes* it — gives the
        UI a natural "undo" without a separate button. Returns one of:
            'useful'      — newly applied
            'not_useful'  — newly applied
            'report'      — newly applied (caller should ask for reason)
            'updated'     — rating changed from another value
            'removed'     — toggled off
        """
        assert rating in ("useful", "not_useful", "report"), rating
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, rating FROM feedback "
                    "WHERE chat_id=%s AND message_id=%s AND user_id=%s",
                    (chat_id, message_id, user_id),
                )
                existing = cur.fetchone()
                if existing and existing["rating"] == rating:
                    cur.execute(
                        "DELETE FROM feedback WHERE id=%s", (existing["id"],)
                    )
                    return "removed"
                if existing:
                    cur.execute(
                        "UPDATE feedback SET rating=%s, reason=NULL, "
                        "created_at=NOW() WHERE id=%s",
                        (rating, existing["id"]),
                    )
                    return "updated"
                cur.execute(
                    "INSERT INTO feedback (chat_id, message_id, user_id, rating) "
                    "VALUES (%s, %s, %s, %s)",
                    (chat_id, message_id, user_id, rating),
                )
                return rating

    def pending_report(
        self, chat_id: int, user_id: int, max_age_minutes: float = 10.0
    ) -> tuple[int, int] | None:
        """If this user recently tapped 🚩 Report and hasn't yet sent a
        reason, return (feedback_id, bot_reply_message_id) so the bot can
        treat their next message as the reason. Otherwise None."""
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, message_id FROM feedback "
                    "WHERE chat_id=%s AND user_id=%s AND rating='report' "
                    "AND reason IS NULL AND created_at >= %s "
                    "ORDER BY created_at DESC LIMIT 1",
                    (chat_id, user_id, cutoff),
                )
                row = cur.fetchone()
        if row is None:
            return None
        return row["id"], row["message_id"]

    # ─── gamification ───────────────────────────────────────────────────

    def update_streak(self, user_id: int) -> dict:
        """Bump the user's streak. Counts at most once per UTC day.

        Returns {current, best} so the caller can show 'you just hit
        N days!' if relevant."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_streak, best_streak, last_streak_date "
                    "FROM users WHERE user_id=%s",
                    (user_id,),
                )
                row = cur.fetchone()
                if row is None:
                    return {"current": 0, "best": 0}
                today = datetime.now(timezone.utc).date()
                last = row["last_streak_date"]
                cur_streak = row["current_streak"] or 0
                best = row["best_streak"] or 0
                if last == today:
                    return {"current": cur_streak, "best": best}
                if last is not None and (today - last).days == 1:
                    cur_streak += 1
                else:
                    cur_streak = 1
                best = max(best, cur_streak)
                cur.execute(
                    "UPDATE users SET current_streak=%s, best_streak=%s, "
                    "last_streak_date=%s WHERE user_id=%s",
                    (cur_streak, best, today, user_id),
                )
                return {"current": cur_streak, "best": best}

    def get_user_stats(self, user_id: int) -> dict:
        """Bundle everything /me needs in one round-trip."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT user_id, username, first_name, last_name, "
                    "language_code, joined_at, last_active_at, query_count, "
                    "current_streak, best_streak, badges "
                    "FROM users WHERE user_id=%s",
                    (user_id,),
                )
                u = cur.fetchone()
                if u is None:
                    return {}

                # Counts from conversations. Refused turns are persisted
                # (so the dashboard transcript matches query_count) but
                # never count toward stats or badges — the user's
                # guardrail: don't reward off-topic / declined messages.
                cur.execute(
                    """SELECT
                        COUNT(*) FILTER (WHERE role='user' AND refused IS NOT TRUE) AS total_q,
                        COUNT(*) FILTER (WHERE role='user' AND refused IS NOT TRUE AND ts >= NOW() - INTERVAL '7 days') AS week_q,
                        COUNT(*) FILTER (WHERE role='user' AND refused IS NOT TRUE AND rerank=TRUE) AS deep_q,
                        COUNT(*) FILTER (WHERE role='user' AND refused IS NOT TRUE AND EXTRACT(hour FROM ts) BETWEEN 0 AND 4) AS night_q,
                        COUNT(*) FILTER (WHERE role='user' AND refused IS NOT TRUE AND EXTRACT(hour FROM ts) BETWEEN 5 AND 7) AS morn_q,
                        COUNT(*) FILTER (
                            WHERE role='assistant'
                            AND refused IS NOT TRUE
                            AND sources::text LIKE %s
                        ) AS pdf_cited
                    FROM conversations WHERE user_id=%s""",
                    ('%"source_type": "pdf"%', user_id),
                )
                counts = cur.fetchone()

                # Languages detected from queries (real ones only).
                cur.execute(
                    "SELECT content FROM conversations "
                    "WHERE user_id=%s AND role='user' AND refused IS NOT TRUE",
                    (user_id,),
                )
                queries = [r["content"] for r in cur.fetchall()]

                # Helpful votes given by this user.
                cur.execute(
                    "SELECT COUNT(*) AS n FROM feedback "
                    "WHERE user_id=%s AND rating='useful'",
                    (user_id,),
                )
                helpful_votes = cur.fetchone()["n"]

                # Top topics from the assistant turns the user got.
                cur.execute(
                    """SELECT s->'metadata'->>'topic' AS topic, COUNT(*) AS n
                       FROM conversations,
                            jsonb_array_elements(COALESCE(sources, '[]'::jsonb)) AS s
                       WHERE user_id=%s
                         AND role='assistant'
                         AND s->'metadata'->>'topic' IS NOT NULL
                         AND s->'metadata'->>'topic' <> ''
                       GROUP BY topic
                       ORDER BY n DESC LIMIT 3""",
                    (user_id,),
                )
                top_topics = [r["topic"] for r in cur.fetchall()]

        return {
            "user_id": u["user_id"],
            "first_name": u["first_name"],
            "last_name": u["last_name"],
            "username": u["username"],
            "joined_at": u["joined_at"],
            "last_active_at": u["last_active_at"],
            "query_count": u["query_count"] or 0,
            "current_streak": u["current_streak"] or 0,
            "best_streak": u["best_streak"] or 0,
            "badges": u["badges"] or [],
            "total_q": counts["total_q"] or 0,
            "week_q": counts["week_q"] or 0,
            "deep_q": counts["deep_q"] or 0,
            "night_q": counts["night_q"] or 0,
            "morn_q": counts["morn_q"] or 0,
            "pdf_cited": counts["pdf_cited"] or 0,
            "helpful_votes": helpful_votes or 0,
            "queries": queries,
            "top_topics": top_topics,
        }

    def set_badges(self, user_id: int, badge_keys: list[str]) -> None:
        from psycopg.types.json import Jsonb
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET badges=%s WHERE user_id=%s",
                    (Jsonb(badge_keys), user_id),
                )

    # ─── events / operational log ───────────────────────────────────────

    def record_event(
        self,
        level: str,
        message: str,
        *,
        logger: str | None = None,
        exc_info: str | None = None,
    ) -> None:
        """Persist one structured log event. Best-effort: any failure is
        swallowed so logging can never break or stall the caller. Secrets
        are scrubbed before insert (the events table is dashboard-visible)."""
        try:
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO events (level, logger, message, exc_info) "
                        "VALUES (%s, %s, %s, %s)",
                        (
                            (level or "")[:16],
                            (logger or None),
                            _scrub_secrets(message) or "",
                            _scrub_secrets(exc_info),
                        ),
                    )
        except Exception:
            pass  # logging must never raise

    def recent_events(self, limit: int = 100, level: str | None = None) -> list[dict]:
        """Most recent events, newest first. Optional exact-level filter."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                if level:
                    cur.execute(
                        "SELECT id, ts, level, logger, message, exc_info FROM events "
                        "WHERE level=%s ORDER BY ts DESC LIMIT %s",
                        (level, limit),
                    )
                else:
                    cur.execute(
                        "SELECT id, ts, level, logger, message, exc_info FROM events "
                        "ORDER BY ts DESC LIMIT %s",
                        (limit,),
                    )
                return cur.fetchall()

    def prune_events(self, keep_days: int = 30) -> int:
        """Delete events older than keep_days. Retention guard so a runaway
        error loop can't fill Neon's free tier. Returns rows deleted."""
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM events WHERE ts < %s", (cutoff,))
                    return cur.rowcount
        except Exception:
            return 0

    def add_report_reason(self, feedback_id: int, reason: str) -> None:
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE feedback SET reason=%s WHERE id=%s",
                    (reason, feedback_id),
                )

    def reset(self, chat_id: int, user_id: int) -> int:
        """Soft-reset the LLM's view of this (chat, user)'s history.

        From the bot's perspective the conversation is gone — `recent_turns`
        filters out anything with `cleared_at` set, so the next message
        starts a fresh thread. From the admin's perspective the rows are
        still in Postgres with a timestamp marking when they were cleared,
        so the dashboard can show the complete audit trail and feedback
        rows never end up orphaned.

        Returns the number of turns the user's reset newly marked (i.e.
        already-cleared turns from a previous reset aren't re-counted)."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE conversations SET cleared_at = NOW() "
                    "WHERE chat_id=%s AND user_id=%s AND cleared_at IS NULL",
                    (chat_id, user_id),
                )
                return cur.rowcount


import logging as _logging

# Loggers whose WARNINGs are known benign noise — kept out of the events
# table so it stays signal-only. (They still print to stdout.) The HF Hub
# "set HF_TOKEN for higher rate limits" nag fires on every model load;
# urllib3/httpx retries are transient network chatter.
_EVENT_IGNORE_LOGGERS = ("huggingface_hub", "urllib3", "httpx", "httpcore", "filelock")


class PostgresLogHandler(_logging.Handler):
    """Mirror WARNING+ log records into the events table so failures
    survive the Space's frequent restarts. Attach once at startup:

        handler = PostgresLogHandler(memory)
        logging.getLogger().addHandler(handler)

    Every write is best-effort (record_event swallows its own errors),
    and a re-entrancy guard stops a DB-layer warning logged during the
    insert from recursing forever."""

    def __init__(self, memory: "ConversationMemory", level: int = _logging.WARNING):
        super().__init__(level=level)
        self._memory = memory
        self._in_emit = False

    def emit(self, record: _logging.LogRecord) -> None:
        if self._in_emit:
            return
        name = record.name or ""
        if any(name == n or name.startswith(n + ".") for n in _EVENT_IGNORE_LOGGERS):
            return
        self._in_emit = True
        try:
            exc_text = None
            if record.exc_info:
                exc_text = self.format(record) if self.formatter else _logging.Formatter().formatException(record.exc_info)
            self._memory.record_event(
                record.levelname,
                record.getMessage(),
                logger=record.name,
                exc_info=exc_text,
            )
        except Exception:
            pass
        finally:
            self._in_emit = False
