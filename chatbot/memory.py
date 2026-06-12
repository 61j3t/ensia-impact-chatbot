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
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterable

import psycopg
from psycopg.rows import dict_row

DEFAULT_DSN = os.getenv(
    "DATABASE_URL",
    "postgresql://ensia:ensia@localhost:5433/ensia_bot",
)

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
                    )
                    for i, t in enumerate(turns)
                ]
                cur.executemany(
                    "INSERT INTO conversations "
                    "(chat_id, user_id, turn_idx, role, content, sources, tg_message_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
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
