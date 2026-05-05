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
                rows = [
                    (chat_id, user_id, next_idx + i, t["role"], t["content"])
                    for i, t in enumerate(turns)
                ]
                cur.executemany(
                    "INSERT INTO conversations "
                    "(chat_id, user_id, turn_idx, role, content) "
                    "VALUES (%s, %s, %s, %s, %s)",
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
                    ORDER BY turn_idx DESC
                    LIMIT %s
                    """,
                    (chat_id, user_id, cutoff, n * 2),
                )
                rows = cur.fetchall()
        return list(reversed(
            [{"role": r["role"], "content": r["content"]} for r in rows]
        ))

    def reset(self, chat_id: int, user_id: int) -> int:
        """Wipe conversation history for this (chat, user). User row stays."""
        with self._conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM conversations WHERE chat_id=%s AND user_id=%s",
                    (chat_id, user_id),
                )
                return cur.rowcount
