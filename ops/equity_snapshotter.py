"""Equity / position time-series snapshotter.

A daemon thread that every 60s writes one row per agent sleeve equity and one
row per open broker position to `data/equity_snapshots.db`. The dashboard
reads these tables to render the time-series charts (sleeve curves, NAV curve,
position heatmap).

Modeled after `ops/heartbeat.py`. The thread is fire-and-forget: a broker
outage or transient SQLite hiccup is logged and swallowed so the loop keeps
running.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from core.types import AgentId, normalize_symbol
from execution.agent_state_tracker import AgentStateTracker
from execution.broker import Broker
from execution.lots import LotLedger

# Agents whose sleeve equity tracks open lots. Manager has no lots, so a
# mark-update would be a no-op and is skipped.
_TRADING_AGENTS: tuple[AgentId, ...] = (AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS)

log = logging.getLogger(__name__)

SNAPSHOT_INTERVAL_SECS: float = 60.0


_DDL_EQUITY = """
CREATE TABLE IF NOT EXISTS equity_snapshots (
  ts TEXT NOT NULL,
  total_nav TEXT,
  haiku_equity TEXT,
  sonnet_equity TEXT,
  opus_equity TEXT,
  manager_equity TEXT,
  haiku_peak TEXT,
  sonnet_peak TEXT,
  opus_peak TEXT,
  manager_peak TEXT
)
"""

_DDL_EQUITY_IDX = "CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots(ts)"

_DDL_POS = """
CREATE TABLE IF NOT EXISTS position_snapshots (
  ts TEXT NOT NULL,
  symbol TEXT NOT NULL,
  qty TEXT NOT NULL,
  market_value TEXT,
  side TEXT,
  unrealized_pl TEXT
)
"""

_DDL_POS_IDX = "CREATE INDEX IF NOT EXISTS idx_pos_ts ON position_snapshots(ts)"

# Per-agent positions, derived from the lot ledger and marked from broker prices.
# Kept alongside (not in place of) the aggregate position_snapshots table so
# existing dashboard queries continue to work unchanged.
_DDL_AGENT_POS = """
CREATE TABLE IF NOT EXISTS agent_position_snapshots (
  ts TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  symbol TEXT NOT NULL,
  qty TEXT NOT NULL,
  avg_entry_price TEXT NOT NULL,
  mark_price TEXT,
  market_value TEXT,
  unrealized_pl TEXT
)
"""

_DDL_AGENT_POS_IDX = (
    "CREATE INDEX IF NOT EXISTS idx_agent_pos_ts "
    "ON agent_position_snapshots(ts, agent_id)"
)


class EquitySnapshotter:
    """Background thread that periodically snapshots sleeve equity and positions."""

    def __init__(
        self,
        db_path: Path,
        agent_state_tracker: AgentStateTracker,
        broker: Broker | None,
        interval_secs: float = SNAPSHOT_INTERVAL_SECS,
        lot_ledger: LotLedger | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._tracker = agent_state_tracker
        self._broker = broker
        self._interval = interval_secs
        # Used to attribute open positions per agent for the historical
        # agent_position_snapshots table. If omitted, per-agent rows are skipped
        # (tests that don't care about attribution can pass broker only).
        self._lot_ledger = lot_ledger
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._init_db()

    def _init_db(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._db_path))
        try:
            conn.execute(_DDL_EQUITY)
            conn.execute(_DDL_EQUITY_IDX)
            conn.execute(_DDL_POS)
            conn.execute(_DDL_POS_IDX)
            conn.execute(_DDL_AGENT_POS)
            conn.execute(_DDL_AGENT_POS_IDX)
            conn.commit()
        finally:
            conn.close()

    def start(self) -> None:
        """Start the snapshot thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="equity-snapshotter"
        )
        self._thread.start()
        log.info(
            "EquitySnapshotter: started (db=%s interval=%.0fs)",
            self._db_path, self._interval,
        )

    def stop(self) -> None:
        """Signal the thread to stop and wait for it (max 5s)."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def tick_once(self, now: datetime | None = None) -> None:
        """Write one snapshot row. Used by tests; production uses the loop."""
        ts = (now if now is not None else datetime.now(UTC)).isoformat()

        # ── NAV + positions (broker-dependent) ────────────────────────────────
        total_nav: Decimal | None = None
        positions_rows: list[tuple[str, str, str, str, str, str]] = []
        marks: dict[str, Decimal] = {}
        if self._broker is not None:
            try:
                acct = self._broker.get_account()
                total_nav = acct.equity
            except Exception:
                log.warning("snapshotter: broker.get_account failed", exc_info=True)
            try:
                positions = list(self._broker.list_positions())
                for p in positions:
                    qty = p.qty
                    market_value = qty * p.current_price
                    side = "long" if qty >= Decimal("0") else "short"
                    unrealized = (p.current_price - p.avg_entry_price) * qty
                    positions_rows.append((
                        ts,
                        p.symbol,
                        str(qty),
                        str(market_value),
                        side,
                        str(unrealized),
                    ))
                    # Build canonical mark dict for per-agent equity recompute.
                    # normalize_symbol collapses "BTC/USD" → "BTCUSD".
                    marks[normalize_symbol(p.symbol)] = p.current_price
            except Exception:
                log.warning("snapshotter: broker.list_positions failed", exc_info=True)

        # ── Drive per-agent equity recompute BEFORE reading sleeves. ─────────
        # Without this, sleeve equity is only recomputed inside dispatch_observation,
        # so unrealized PnL on held lots stays frozen between agent observations
        # (which are infrequent, gated by budget, and skipped by fingerprint).
        for agent_id in _TRADING_AGENTS:
            try:
                self._tracker.update_on_mark(agent_id, marks)
            except Exception:
                log.exception("snapshotter: update_on_mark(%s) failed", agent_id)

        # ── Sleeve equities (now reflect fresh marks) ─────────────────────────
        sleeves: dict[AgentId, tuple[Decimal, Decimal]] = {}
        for agent_id in (AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS, AgentId.MANAGER):
            try:
                state = self._tracker.get_state(agent_id)
                sleeves[agent_id] = (state.sleeve_equity, state.sleeve_peak_equity)
            except Exception:
                log.exception("snapshotter: tracker.get_state(%s) failed", agent_id)
                sleeves[agent_id] = (Decimal("0"), Decimal("0"))

        # ── Per-agent position rows (historical attribution) ──────────────────
        agent_pos_rows = self._build_agent_position_rows(ts, marks)

        try:
            self._write_rows(ts, total_nav, sleeves, positions_rows, agent_pos_rows)
        except Exception:
            log.exception("snapshotter: write failed")

    def _build_agent_position_rows(
        self,
        ts: str,
        marks: dict[str, Decimal],
    ) -> list[tuple[str, str, str, str, str, str | None, str | None, str | None]]:
        """Aggregate open lots → one row per (agent, symbol).

        qty is the sum of remaining_qty across open lots; avg_entry_price is
        the qty-weighted entry. Mark is looked up by canonical symbol; if
        absent, mark/value/unrealized fields are NULL (so the row still
        records the position but flags missing data).
        """
        if self._lot_ledger is None:
            return []

        # (agent, symbol) → (qty, weighted_cost)
        agg: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
        try:
            for lot in self._lot_ledger.all_lots():
                if lot.is_closed or lot.remaining_qty <= Decimal("0"):
                    continue
                key = (str(lot.agent_id), lot.symbol)
                qty, cost = agg.get(key, (Decimal("0"), Decimal("0")))
                agg[key] = (
                    qty + lot.remaining_qty,
                    cost + lot.remaining_qty * lot.entry_price,
                )
        except Exception:
            log.exception("snapshotter: building agent position rows failed")
            return []

        rows: list[tuple[str, str, str, str, str, str | None, str | None, str | None]] = []
        for (agent_id, symbol), (qty, weighted_cost) in agg.items():
            avg_entry = (weighted_cost / qty) if qty > Decimal("0") else Decimal("0")
            mark = marks.get(normalize_symbol(symbol)) or marks.get(symbol)
            if mark is not None:
                market_value = qty * mark
                unrealized = (mark - avg_entry) * qty
                mark_s: str | None = str(mark)
                mv_s: str | None = str(market_value)
                upl_s: str | None = str(unrealized)
            else:
                mark_s = mv_s = upl_s = None
            rows.append((
                ts, agent_id, symbol, str(qty), str(avg_entry),
                mark_s, mv_s, upl_s,
            ))
        return rows

    def _write_rows(
        self,
        ts: str,
        total_nav: Decimal | None,
        sleeves: dict[AgentId, tuple[Decimal, Decimal]],
        positions_rows: list[tuple[str, str, str, str, str, str]],
        agent_pos_rows: list[tuple[str, str, str, str, str, str | None, str | None, str | None]] | None = None,
    ) -> None:
        conn = sqlite3.connect(str(self._db_path))
        try:
            haiku_eq, haiku_pk = sleeves.get(AgentId.HAIKU, (Decimal("0"), Decimal("0")))
            sonnet_eq, sonnet_pk = sleeves.get(AgentId.SONNET, (Decimal("0"), Decimal("0")))
            opus_eq, opus_pk = sleeves.get(AgentId.OPUS, (Decimal("0"), Decimal("0")))
            mgr_eq, mgr_pk = sleeves.get(AgentId.MANAGER, (Decimal("0"), Decimal("0")))
            conn.execute(
                "INSERT INTO equity_snapshots "
                "(ts, total_nav, haiku_equity, sonnet_equity, opus_equity, "
                "manager_equity, haiku_peak, sonnet_peak, opus_peak, manager_peak) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    str(total_nav) if total_nav is not None else None,
                    str(haiku_eq), str(sonnet_eq), str(opus_eq), str(mgr_eq),
                    str(haiku_pk), str(sonnet_pk), str(opus_pk), str(mgr_pk),
                ),
            )
            if positions_rows:
                conn.executemany(
                    "INSERT INTO position_snapshots "
                    "(ts, symbol, qty, market_value, side, unrealized_pl) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    positions_rows,
                )
            if agent_pos_rows:
                conn.executemany(
                    "INSERT INTO agent_position_snapshots "
                    "(ts, agent_id, symbol, qty, avg_entry_price, "
                    "mark_price, market_value, unrealized_pl) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    agent_pos_rows,
                )
            conn.commit()
        finally:
            conn.close()

    def prune_old_snapshots(self) -> None:
        """Trim equity_snapshots and position_snapshots in place.

        Retention policy:
        - Last 7 days  : full 1-minute resolution — untouched.
        - 7–30 days    : downsampled to one row per 5-minute bucket (earliest wins).
        - Older than 30 days : dropped entirely.

        Safe to call at any time; uses a single SQLite connection and commits
        atomically. A failure is logged and swallowed so the main loop continues.
        """
        _5MIN_BUCKET = (
            "strftime('%Y-%m-%dT%H:', ts) || "
            "printf('%02d', (CAST(strftime('%M', ts) AS INTEGER) / 5) * 5)"
        )
        try:
            # `timeout=10` waits up to 10s for any concurrent reader (dashboard
            # polling) to release its lock before raising "database is locked".
            conn = sqlite3.connect(str(self._db_path), timeout=10.0)
            try:
                for table in ("equity_snapshots", "position_snapshots", "agent_position_snapshots"):
                    # Drop rows older than 30 days.
                    conn.execute(
                        f"DELETE FROM {table} WHERE ts < datetime('now', '-30 days')"
                    )
                    # In the 7–30 day window keep only the earliest row per 5-min bucket.
                    conn.execute(
                        f"""
                        DELETE FROM {table}
                        WHERE ts < datetime('now', '-7 days')
                          AND ts NOT IN (
                            SELECT MIN(ts)
                            FROM {table}
                            WHERE ts < datetime('now', '-7 days')
                            GROUP BY {_5MIN_BUCKET}
                          )
                        """
                    )
                # Commit the deletes before attempting to reclaim WAL space.
                # If we don't commit first and the checkpoint then fails, the
                # whole transaction is lost and the prune effectively no-ops.
                conn.commit()
            finally:
                conn.close()
            log.info("EquitySnapshotter: pruned old snapshots (7d full / 7-30d 5-min / >30d dropped)")
        except Exception:
            log.exception("EquitySnapshotter: prune_old_snapshots failed")

    def _run_loop(self) -> None:
        last_prune = datetime.now(UTC) - timedelta(hours=25)  # prune on first tick
        while not self._stop_event.is_set():
            try:
                self.tick_once()
            except Exception:
                log.exception("EquitySnapshotter: tick failed")
            # Run retention prune once per day.
            now = datetime.now(UTC)
            if (now - last_prune) >= timedelta(hours=24):
                self.prune_old_snapshots()
                last_prune = now
            self._stop_event.wait(self._interval)
