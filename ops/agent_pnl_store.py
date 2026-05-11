"""Persistence for daily per-agent P&L attribution snapshots.

T1.5 / Plan 2c. Writes one row per (date, agent) into `agent_pnl_daily`
in `data/equity_snapshots.db` (same DB file as the existing equity
snapshotter for operational simplicity). Decimal stored as TEXT to
match the existing convention in this DB; never read back as float.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from core.types import AgentId
from ops.attribution import PnLBreakdown

log = logging.getLogger(__name__)


_DDL = """
CREATE TABLE IF NOT EXISTS agent_pnl_daily (
  date         TEXT NOT NULL,
  agent_id     TEXT NOT NULL,
  realized     TEXT NOT NULL,
  unrealized   TEXT NOT NULL,
  total        TEXT NOT NULL,
  num_open     INTEGER NOT NULL,
  num_closed   INTEGER NOT NULL,
  PRIMARY KEY (date, agent_id)
)
"""


@dataclass(frozen=True)
class AgentPnLRow:
    snapshot_date: date
    agent_id: AgentId
    realized: Decimal
    unrealized: Decimal
    total: Decimal
    num_open: int
    num_closed: int


class AgentPnLStore:
    """SQLite store for daily per-agent P&L attribution snapshots."""

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute(_DDL)
            conn.commit()

    def upsert_snapshot(
        self,
        snapshot_date: date,
        agent_id: AgentId,
        breakdown: PnLBreakdown,
    ) -> None:
        """Insert or replace the (date, agent_id) row.

        Same-day re-runs (e.g. crash recovery firing the daily job twice)
        update in place rather than duplicating rows.
        """
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO agent_pnl_daily "
                "(date, agent_id, realized, unrealized, total, num_open, num_closed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_date.isoformat(),
                    str(agent_id.value),
                    str(breakdown.realized),
                    str(breakdown.unrealized),
                    str(breakdown.total),
                    breakdown.num_open_lots,
                    breakdown.num_closed_lots,
                ),
            )
            conn.commit()

    def write_all(
        self,
        snapshot_date: date,
        breakdowns: dict[AgentId, PnLBreakdown],
    ) -> None:
        """Convenience: write one row per agent in `breakdowns`."""
        for aid, br in breakdowns.items():
            self.upsert_snapshot(snapshot_date, aid, br)

    def recent(
        self,
        agent_id: AgentId | None = None,
        limit: int = 30,
    ) -> list[AgentPnLRow]:
        """Read the N most-recent rows, optionally filtered by agent."""
        with sqlite3.connect(str(self._db_path)) as conn:
            if agent_id is None:
                cur = conn.execute(
                    "SELECT date, agent_id, realized, unrealized, total, "
                    "num_open, num_closed FROM agent_pnl_daily "
                    "ORDER BY date DESC LIMIT ?",
                    (limit,),
                )
            else:
                cur = conn.execute(
                    "SELECT date, agent_id, realized, unrealized, total, "
                    "num_open, num_closed FROM agent_pnl_daily "
                    "WHERE agent_id = ? ORDER BY date DESC LIMIT ?",
                    (str(agent_id.value), limit),
                )
            return [_row_from_db(r) for r in cur.fetchall()]


def _row_from_db(r: Iterable[object]) -> AgentPnLRow:
    d, aid, real, unreal, total, nopen, nclosed = tuple(r)
    return AgentPnLRow(
        snapshot_date=date.fromisoformat(str(d)),
        agent_id=AgentId(str(aid)),
        realized=Decimal(str(real)),
        unrealized=Decimal(str(unreal)),
        total=Decimal(str(total)),
        num_open=int(nopen),  # type: ignore[arg-type]
        num_closed=int(nclosed),  # type: ignore[arg-type]
    )
