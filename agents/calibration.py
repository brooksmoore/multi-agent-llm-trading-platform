"""Brier-style calibration tracker: conviction scores vs. realized trade outcomes."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any


class CalibrationTracker:
    """Tracks conviction vs. outcome to compute Brier scores per agent."""

    _OUTCOME_SCORE: dict[str, float] = {"win": 1.0, "loss": 0.0, "flat": 0.5}

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS calibration (
                    intent_id  TEXT PRIMARY KEY,
                    agent_id   TEXT NOT NULL,
                    conviction INTEGER NOT NULL,
                    outcome    TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def record(
        self,
        intent_id: str,
        agent_id: str,
        conviction: int,
        outcome: str,
    ) -> None:
        """Insert or replace a calibration record."""
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO calibration (intent_id, agent_id, conviction, outcome)
                VALUES (?, ?, ?, ?)
                """,
                (intent_id, agent_id, conviction, outcome),
            )
            self._conn.commit()

    def brier_score(self, agent_id: str | None = None) -> float:
        """Return the mean Brier score across all matching records.

        Brier = mean((conviction/10 - outcome_val)^2).
        Returns 0.0 when no rows match the filter.
        """
        query = "SELECT conviction, outcome FROM calibration"
        params: list[str] = []
        if agent_id is not None:
            query += " WHERE agent_id = ?"
            params.append(agent_id)

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()

        if not rows:
            return 0.0

        total: float = sum(
            (row["conviction"] / 10.0 - self._OUTCOME_SCORE.get(row["outcome"], 0.5)) ** 2
            for row in rows
        )
        return total / len(rows)

    def calibration_table(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        """Return per-conviction-bucket calibration stats.

        Each bucket dict: {bucket, n, win_rate (float|None), brier (float|None)}.
        Buckets: 'low (1-3)', 'medium (4-6)', 'high (7-10)'.
        """
        query = "SELECT conviction, outcome FROM calibration"
        params: list[str] = []
        if agent_id is not None:
            query += " WHERE agent_id = ?"
            params.append(agent_id)

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()

        def _bucket(conviction: int) -> str:
            if conviction <= 3:
                return "low (1-3)"
            if conviction <= 6:
                return "medium (4-6)"
            return "high (7-10)"

        buckets: dict[str, list[tuple[int, str]]] = {
            "low (1-3)": [],
            "medium (4-6)": [],
            "high (7-10)": [],
        }
        for row in rows:
            buckets[_bucket(row["conviction"])].append(
                (row["conviction"], row["outcome"])
            )

        result: list[dict[str, Any]] = []
        for bucket_name, entries in buckets.items():
            if not entries:
                result.append(
                    {"bucket": bucket_name, "n": 0, "win_rate": None, "brier": None}
                )
                continue
            n = len(entries)
            wins = sum(1 for _, outcome in entries if outcome == "win")
            win_rate = wins / n
            brier = sum(
                (conv / 10.0 - self._OUTCOME_SCORE.get(outcome, 0.5)) ** 2
                for conv, outcome in entries
            ) / n
            result.append(
                {"bucket": bucket_name, "n": n, "win_rate": win_rate, "brier": brier}
            )
        return result

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()
