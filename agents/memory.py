"""SQLite-backed agent memory: key-value facts, daily journals, intent history."""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, date, datetime
from pathlib import Path

from core.types import AgentId, IntentId


class AgentMemory:
    """Persistent per-agent store: key-value facts, daily journals, intent log."""

    def __init__(self, db_path: str | Path, agent_id: AgentId) -> None:
        self._agent_id = str(agent_id)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                agent_id   TEXT NOT NULL,
                key        TEXT NOT NULL,
                value      TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (agent_id, key)
            );
            CREATE TABLE IF NOT EXISTS journals (
                agent_id TEXT NOT NULL,
                date     TEXT NOT NULL,
                content  TEXT NOT NULL,
                PRIMARY KEY (agent_id, date)
            );
            CREATE TABLE IF NOT EXISTS intent_log (
                intent_id  TEXT PRIMARY KEY,
                agent_id   TEXT NOT NULL,
                symbol     TEXT NOT NULL,
                action     TEXT NOT NULL,
                conviction INTEGER NOT NULL,
                rationale  TEXT NOT NULL,
                outcome    TEXT,
                logged_at  TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def remember(self, key: str, value: str) -> None:
        """Upsert a key-value fact for this agent."""
        now = datetime.now(UTC).isoformat()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO memories (agent_id, key, value, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (agent_id, key)
                DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (self._agent_id, key, value, now),
            )
            self._conn.commit()

    def recall(self, key: str) -> str | None:
        """Return the stored value for *key*, or None if absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM memories WHERE agent_id = ? AND key = ?",
                (self._agent_id, key),
            ).fetchone()
        return row["value"] if row else None

    def write_journal(self, entry_date: date, content: str) -> None:
        """Write (or overwrite) the daily journal entry for *entry_date*."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO journals (agent_id, date, content)
                VALUES (?, ?, ?)
                ON CONFLICT (agent_id, date)
                DO UPDATE SET content = excluded.content
                """,
                (self._agent_id, entry_date.isoformat(), content),
            )
            self._conn.commit()

    def read_journal(self, entry_date: date) -> str | None:
        """Return the journal entry for *entry_date*, or None if absent."""
        with self._lock:
            row = self._conn.execute(
                "SELECT content FROM journals WHERE agent_id = ? AND date = ?",
                (self._agent_id, entry_date.isoformat()),
            ).fetchone()
        return row["content"] if row else None

    def record_intent(
        self,
        intent_id: IntentId,
        symbol: str,
        action: str,
        conviction: int,
        rationale: str,
        ts: datetime,
    ) -> None:
        """Insert an intent into the log (ignored if the ID already exists)."""
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO intent_log
                    (intent_id, agent_id, symbol, action, conviction, rationale, outcome, logged_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    str(intent_id),
                    self._agent_id,
                    symbol,
                    action,
                    conviction,
                    rationale,
                    ts.isoformat(),
                ),
            )
            self._conn.commit()

    def record_outcome(self, intent_id: IntentId, outcome: str) -> None:
        """Set the realized outcome for a previously recorded intent."""
        with self._lock:
            self._conn.execute(
                "UPDATE intent_log SET outcome = ? WHERE intent_id = ?",
                (outcome, str(intent_id)),
            )
            self._conn.commit()

    def recent_intents_rows(self, n: int = 10) -> list[dict[str, str | int | None]]:
        """Return the *n* most recent intents as structured rows (for dashboard)."""
        with self._lock:
            try:
                rows = self._conn.execute(
                    """
                    SELECT intent_id, symbol, action, conviction, rationale, outcome, logged_at
                    FROM intent_log
                    WHERE agent_id = ?
                    ORDER BY logged_at DESC
                    LIMIT ?
                    """,
                    (self._agent_id, n),
                ).fetchall()
            except sqlite3.ProgrammingError:
                # DB closed during shutdown; dashboard polling can race.
                return []
        return [
            {
                "intent_id": row["intent_id"],
                "symbol": row["symbol"],
                "action": row["action"],
                "conviction": row["conviction"],
                "rationale": row["rationale"],
                "outcome": row["outcome"],
                "logged_at": row["logged_at"],
            }
            for row in rows
        ]

    def recent_intents_summary(self, n: int = 3) -> str:
        """Return a human-readable summary of the *n* most recent intents."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT action, symbol, conviction, outcome, rationale
                FROM intent_log
                WHERE agent_id = ?
                ORDER BY logged_at DESC
                LIMIT ?
                """,
                (self._agent_id, n),
            ).fetchall()
        if not rows:
            return "No recent intents."
        lines = [
            f"  {row['action']} {row['symbol']} (conv={row['conviction']}) "
            f"→ {row['outcome']}: {row['rationale'][:80]}"
            for row in rows
        ]
        return "\n".join(lines)

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()
