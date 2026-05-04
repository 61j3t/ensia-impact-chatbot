"""SQLite-backed conversation memory for the chatbot.

Each turn is keyed by (chat_id, user_id) and stored with a monotonic
turn_idx so retrieval order is preserved. Memory is intentionally simple
— no summarization, no embedding, just rolling window of recent turns.

Schema:
    conversations(chat_id, user_id, turn_idx, role, content, ts)

Public API:
    ConversationMemory.add_turns(chat_id, user_id, [{role, content}, ...])
    ConversationMemory.recent_turns(chat_id, user_id, n=5, max_age_hours=24)
    ConversationMemory.reset(chat_id, user_id) → count of turns deleted
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data/conversations.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    chat_id  INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    turn_idx INTEGER NOT NULL,
    role     TEXT    NOT NULL CHECK (role IN ('user', 'assistant')),
    content  TEXT    NOT NULL,
    ts       TEXT    NOT NULL,
    PRIMARY KEY (chat_id, user_id, turn_idx)
);

CREATE INDEX IF NOT EXISTS idx_conv_recent
    ON conversations (chat_id, user_id, turn_idx DESC);
"""


class ConversationMemory:
    """Per-(chat, user) turn-by-turn memory with TTL on reads.

    Concurrency note: SQLite serializes writes via its file lock; for the
    bot's single-process model that's fine. If we ever multi-process,
    bump `timeout` and consider WAL mode.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_SCHEMA)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def add_turns(
        self,
        chat_id: int,
        user_id: int,
        turns: Iterable[dict],
    ) -> None:
        """Append a sequence of {role, content} dicts atomically."""
        turns = list(turns)
        if not turns:
            return
        with self._conn() as c:
            cur = c.execute(
                "SELECT COALESCE(MAX(turn_idx), -1) FROM conversations "
                "WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            next_idx = cur.fetchone()[0] + 1
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            rows = [
                (chat_id, user_id, next_idx + i, t["role"], t["content"], now)
                for i, t in enumerate(turns)
            ]
            c.executemany(
                "INSERT INTO conversations(chat_id, user_id, turn_idx, role, content, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
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
        ordered oldest → newest. Each row is `{role, content}`.

        Old turns past the TTL are filtered at read time but not deleted —
        cheap, and keeps history available if you bump the TTL later.
        """
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
        ).isoformat(timespec="seconds")
        with self._conn() as c:
            cur = c.execute(
                "SELECT role, content FROM conversations "
                "WHERE chat_id=? AND user_id=? AND ts >= ? "
                "ORDER BY turn_idx DESC LIMIT ?",
                (chat_id, user_id, cutoff, n * 2),
            )
            rows = cur.fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def reset(self, chat_id: int, user_id: int) -> int:
        """Wipe all history for this (chat, user). Returns rows deleted."""
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM conversations WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )
            return cur.rowcount
