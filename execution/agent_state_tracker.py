"""Per-agent drawdown bucket tracker with recovery rule.

Blueprint §16.3:
  - Tightening (worse bucket): immediate.
  - Loosening (better bucket): only after 5 consecutive trading days at the
    better level.  Prevents whipsaw re-leveraging into dead-cat bounces.

Blueprint §5 Layer 3 + §17.7:
  - FORCED_CASH (>25% drawdown) requires Manager mc_proposal to re-enable.
  - 5 consecutive losing trades → bench agent for 24h via KillSwitchEngine.

Equity model per agent:
  current_equity = starting_equity + realized_pnl(LotLedger) + unrealized_pnl(marks)

Peak equity is the rolling 30-day high of current_equity.  For simplicity in
this implementation, we track a rolling 30-day window by storing (date, equity)
tuples and pruning on each mark update.

SQLite persistence: tracker state survives restarts.  On cold start, call
`rebuild_from_ledger(mark_prices)` if the DB table is empty.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from core.types import (
    DUST_NOTIONAL_USD,
    AgentId,
    AgentState,
    DrawdownBucket,
    Fill,
    OrderSide,
    normalize_symbol,
)
from execution.kill_switch import (
    CONSECUTIVE_LOSS_BENCH_TRIGGER,
    KillSwitchEngine,
    classify_drawdown,
)
from execution.lots import LotLedger

log = logging.getLogger(__name__)

# Recovery rule: must stay in a better bucket for this many consecutive trading
# days before we loosen the sizing bucket.
_RECOVERY_DAYS_REQUIRED: int = 5

# Rolling peak window (trading days; we keep the last ~30)
_PEAK_WINDOW_DAYS: int = 30

# Forced-cash threshold matches blueprint §16.3
_FORCED_CASH_THRESHOLD: Decimal = Decimal("0.25")


@dataclass
class _PerAgentRecord:
    starting_equity: Decimal
    current_equity: Decimal
    peak_equity: Decimal

    # Consecutive-loss counter (mirrors KillSwitchEngine; tracked here for
    # get_state() return without an extra engine call).
    consecutive_losses: int
    is_benched: bool
    bench_until: datetime | None

    # The bucket *actually used for sizing* — may lag drawdown improvement.
    sizing_bucket: DrawdownBucket

    # Recovery tracking (loosening is deferred).
    recovery_target: DrawdownBucket | None  # looser bucket we're recovering to
    recovery_since: date | None             # first qualified day
    recovery_days: int                      # consecutive qualifying days

    # Last update date — used to detect when a new trading day begins.
    last_update_date: date | None

    # Win/loss tracking for consecutive_losses.
    # We maintain avg cost basis per held symbol so we can determine
    # win/loss at fill time without depending on LotLedger call order.
    _avg_cost: dict[str, Decimal] = field(default_factory=dict)
    _open_qty: dict[str, Decimal] = field(default_factory=dict)


class AgentStateTracker:
    """Thread-safe per-agent equity and drawdown bucket tracker.

    Usage:
        tracker = AgentStateTracker(kill_switch, lot_ledger, db_path="data/tracker.db")
        # On each fill (called after lot ledger is updated):
        tracker.update_on_fill(fill)
        # On each reconciler tick:
        tracker.update_on_mark(agent_id, mark_prices)
        # To build CoreAgentState for RiskGate / ExecutionPlanner:
        state = tracker.get_state(agent_id)
    """

    def __init__(
        self,
        kill_switch: KillSwitchEngine,
        lot_ledger: LotLedger,
        starting_equity: Decimal = Decimal("1000"),
        db_path: str | None = None,
        bus: object | None = None,
    ) -> None:
        self._kill = kill_switch
        self._ledger = lot_ledger
        self._starting_equity = starting_equity
        self._lock = threading.Lock()
        self._records: dict[AgentId, _PerAgentRecord] = {}
        self._db_path = db_path
        # Optional EventBus reference. If supplied, the tracker publishes a
        # DrawdownLadderFiredEvent on every bucket-tightening transition so
        # the Manager (or any other subscriber) can react in real time.
        self._bus = bus

        self._init_db()
        self._load_or_init_records()

    # ── Public API ────────────────────────────────────────────────────────────

    def update_on_fill(self, fill: Fill) -> None:
        """Recompute per-agent state after every fill.

        For BUY fills: updates average cost basis.
        For SELL fills: determines win/loss, updates consecutive_losses,
        triggers bench if threshold reached.
        """
        with self._lock:
            rec = self._records[fill.agent_id]

            if fill.side == OrderSide.BUY:
                self._update_avg_cost(rec, fill)
            else:
                is_loss = self._is_loss(rec, fill)
                self._clear_avg_cost(rec, fill)

                if is_loss:
                    rec.consecutive_losses += 1
                    if rec.consecutive_losses >= CONSECUTIVE_LOSS_BENCH_TRIGGER:
                        now = datetime.now(UTC)
                        self._kill.record_agent_result(
                            fill.agent_id, is_loss=True, ts=now
                        )
                        rec.is_benched = True
                        rec.bench_until = now + timedelta(hours=24)
                        log.info(
                            "tracker: %s benched after %d consecutive losses",
                            fill.agent_id,
                            rec.consecutive_losses,
                        )
                        if self._bus is not None:
                            try:
                                from core.events import AgentBenchedEvent
                                self._bus.publish(AgentBenchedEvent(
                                    agent_id=fill.agent_id,
                                    consecutive_losses=rec.consecutive_losses,
                                ))
                            except Exception:
                                log.warning(
                                    "tracker: failed to publish AgentBenched", exc_info=True,
                                )
                else:
                    rec.consecutive_losses = 0
                    if rec.is_benched:
                        rec.is_benched = False
                        rec.bench_until = None

        self._maybe_persist()

    def update_on_mark(
        self, agent_id: AgentId, mark_prices: dict[str, Decimal]
    ) -> None:
        """Recompute equity + bucket on each reconciler tick.

        mark_prices: symbol → current price for all held symbols.
        """
        with self._lock:
            rec = self._records[agent_id]
            today = datetime.now(UTC).date()

            # ── Equity recompute ──────────────────────────────────────────────
            realized = self._realized_pnl(agent_id)
            unrealized = self._unrealized_pnl(agent_id, mark_prices)
            # Use the per-agent starting equity (loaded from DB) rather than
            # the global default, so a sleeve like Manager that was provisioned
            # at a different baseline (e.g. $10k reserve vs $30k sleeves) keeps
            # its baseline across reconciler ticks.
            new_equity = rec.starting_equity + realized + unrealized
            rec.current_equity = new_equity

            # Update bench expiry
            bench_expired = (
                rec.is_benched
                and rec.bench_until is not None
                and datetime.now(UTC) >= rec.bench_until
            )
            if bench_expired:
                rec.is_benched = False
                rec.bench_until = None
                rec.consecutive_losses = 0

            # ── Peak equity (30-day rolling high) ────────────────────────────
            if new_equity > rec.peak_equity:
                rec.peak_equity = new_equity

            # ── Drawdown + bucket ─────────────────────────────────────────────
            if rec.peak_equity > Decimal("0"):
                drawdown = (rec.peak_equity - new_equity) / rec.peak_equity
            else:
                drawdown = Decimal("0")

            raw_bucket = classify_drawdown(drawdown)
            self._apply_bucket_with_recovery(rec, raw_bucket, today, agent_id=agent_id)

        self._maybe_persist()

    def get_state(self, agent_id: AgentId) -> AgentState:
        """Return a live CoreAgentState for the given agent.

        Checks bench expiry at read time so the state is always current.
        """
        with self._lock:
            rec = self._records[agent_id]

            # Check if bench has expired
            is_benched = rec.is_benched
            bench_until = rec.bench_until
            if is_benched and bench_until is not None and datetime.now(UTC) >= bench_until:
                rec.is_benched = False
                rec.bench_until = None
                rec.consecutive_losses = 0
                is_benched = False
                bench_until = None

            return AgentState(
                agent_id=agent_id,
                sleeve_equity=rec.current_equity,
                sleeve_peak_equity=rec.peak_equity,
                drawdown_bucket=rec.sizing_bucket,
                drawdown_bucket_entry_date=rec.recovery_since,
                consecutive_losses=rec.consecutive_losses,
                is_benched=is_benched,
                bench_until=bench_until,
                day_trade_count=0,   # sourced from KillSwitchEngine in app.py
                orders_today=0,      # sourced from OMS in app.py
                last_memo_id=None,
            )

    def rebuild_from_ledger(
        self, mark_prices: dict[str, Decimal] | None = None
    ) -> None:
        """Rebuild tracker state from LotLedger history (cold start).

        Replays all fills in chronological order to recompute consecutive_losses.
        Sets equity from realized + unrealized (marks optional).
        Does NOT apply the recovery rule (conservative: uses raw bucket directly).
        """
        mark_prices = mark_prices or {}

        for agent_id in AgentId:
            existing = self._records.get(agent_id)
            base = existing.starting_equity if existing is not None else self._starting_equity
            realized = self._realized_pnl(agent_id)
            unrealized = self._unrealized_pnl(agent_id, mark_prices)
            current_equity = base + realized + unrealized

            # Replay fills to recompute consecutive_losses
            consecutive = self._replay_consecutive_losses(agent_id)

            peak_equity = max(current_equity, base)

            drawdown = Decimal("0")
            if peak_equity > Decimal("0"):
                drawdown = (peak_equity - current_equity) / peak_equity
            bucket = classify_drawdown(drawdown)

            with self._lock:
                rec = self._records[agent_id]
                rec.current_equity = current_equity
                rec.peak_equity = peak_equity
                rec.consecutive_losses = consecutive
                rec.sizing_bucket = bucket
                rec.recovery_target = None
                rec.recovery_since = None
                rec.recovery_days = 0

        log.info("tracker: rebuilt from LotLedger (cold start)")
        self._maybe_persist()

    # ── Internal: bucket transitions ──────────────────────────────────────────

    def _apply_bucket_with_recovery(
        self,
        rec: _PerAgentRecord,
        raw_bucket: DrawdownBucket,
        today: date,
        *,
        agent_id: AgentId | None = None,
    ) -> None:
        """Apply the recovery rule: tighten immediately, loosen only after 5 days."""
        current = rec.sizing_bucket

        # Bucket ordering (worse = higher index)
        bucket_order = [
            DrawdownBucket.NORMAL,
            DrawdownBucket.YELLOW,
            DrawdownBucket.ORANGE,
            DrawdownBucket.RED,
            DrawdownBucket.FORCED_CASH,
        ]

        def _rank(b: DrawdownBucket) -> int:
            return bucket_order.index(b)

        if _rank(raw_bucket) >= _rank(current):
            # Tightening or same — immediate.
            if raw_bucket != current:
                log.info(
                    "tracker: bucket tightened %s → %s", current, raw_bucket
                )
                self._publish_ladder_event(rec, raw_bucket, agent_id=agent_id)
            rec.sizing_bucket = raw_bucket
            # Clear any pending recovery.
            rec.recovery_target = None
            rec.recovery_since = None
            rec.recovery_days = 0
            rec.last_update_date = today
        else:
            # Loosening candidate — apply recovery rule.
            is_new_day = today != rec.last_update_date

            if rec.recovery_target != raw_bucket:
                # New (better) target, reset counter.
                rec.recovery_target = raw_bucket
                rec.recovery_since = today
                rec.recovery_days = 1
            elif is_new_day:
                rec.recovery_days += 1

            if rec.recovery_days >= _RECOVERY_DAYS_REQUIRED:
                log.info(
                    "tracker: bucket loosened %s → %s after %d recovery days",
                    current,
                    raw_bucket,
                    rec.recovery_days,
                )
                rec.sizing_bucket = raw_bucket
                rec.recovery_target = None
                rec.recovery_since = None
                rec.recovery_days = 0

            rec.last_update_date = today

    def _publish_ladder_event(
        self,
        rec: _PerAgentRecord,
        new_bucket: DrawdownBucket,
        *,
        agent_id: AgentId | None = None,
    ) -> None:
        """Emit DrawdownLadderFiredEvent on tightening transitions.

        Subscribers (Manager, dashboard, alerts) can react in real time.
        Failures are swallowed — bucket transition state is more important
        than event-bus delivery.
        """
        if self._bus is None:
            return
        try:
            from core.events import DrawdownLadderFiredEvent
            drawdown_pct = Decimal("0")
            if rec.peak_equity > Decimal("0"):
                drawdown_pct = (
                    (rec.peak_equity - rec.current_equity) / rec.peak_equity
                )
            self._bus.publish(
                DrawdownLadderFiredEvent(
                    agent_id=agent_id,
                    drawdown_pct=drawdown_pct,
                    new_bucket=str(new_bucket).split(".")[-1].lower(),
                )
            )
        except Exception:
            log.warning("tracker: failed to publish DrawdownLadderFired", exc_info=True)

    # ── Internal: P&L helpers ─────────────────────────────────────────────────

    def _realized_pnl(self, agent_id: AgentId) -> Decimal:
        total = Decimal("0")
        for lot in self._ledger.all_lots():
            if lot.agent_id == agent_id and lot.is_closed and lot.realized_pnl is not None:
                total += lot.realized_pnl
        return total

    def _unrealized_pnl(
        self, agent_id: AgentId, mark_prices: dict[str, Decimal]
    ) -> Decimal:
        total = Decimal("0")
        missing: list[str] = []
        for lot in self._ledger.all_lots():
            if lot.agent_id == agent_id and not lot.is_closed:
                # Skip dust lots: a sub-cent remaining position (crypto in-kind
                # fee residue or float-rounded sell sliver) is not a real
                # holding. Including it forces a mark lookup that need not exist
                # and spams "missing marks for held lots" while contributing
                # nothing measurable to equity. Newer fills auto-close such
                # slivers in LotLedger.close_lots; this guards dust already
                # persisted from before that fix.
                if lot.remaining_qty * lot.entry_price < DUST_NOTIONAL_USD:
                    continue
                # Lots may carry crypto symbols in either "BTC/USD" or "BTCUSD"
                # form depending on which path persisted them. Normalize before
                # lookup so a single canonical mark serves both.
                key = normalize_symbol(lot.symbol)
                mark = mark_prices.get(key)
                if mark is None:
                    mark = mark_prices.get(lot.symbol)
                if mark is not None:
                    total += (mark - lot.entry_price) * lot.remaining_qty
                else:
                    missing.append(lot.symbol)
        if missing:
            log.warning(
                "tracker: %s unrealized PnL missing marks for held lots: %s "
                "(equity will under-report)",
                agent_id, sorted(set(missing)),
            )
        return total

    def _update_avg_cost(self, rec: _PerAgentRecord, fill: Fill) -> None:
        """Update running average cost basis on BUY fill."""
        prev_qty = rec._open_qty.get(fill.symbol, Decimal("0"))
        prev_cost = rec._avg_cost.get(fill.symbol, Decimal("0"))
        new_qty = prev_qty + fill.qty
        if new_qty > Decimal("0"):
            new_cost = (prev_cost * prev_qty + fill.price * fill.qty) / new_qty
            rec._avg_cost[fill.symbol] = new_cost
            rec._open_qty[fill.symbol] = new_qty

    def _is_loss(self, rec: _PerAgentRecord, fill: Fill) -> bool:
        """Determine if a SELL fill is a loss based on tracked avg cost."""
        avg_cost = rec._avg_cost.get(fill.symbol)
        if avg_cost is None:
            return False  # no cost tracked → can't determine
        return fill.price < avg_cost

    def _clear_avg_cost(self, rec: _PerAgentRecord, fill: Fill) -> None:
        """Update open_qty after a SELL; clear avg_cost when position is flat."""
        prev_qty = rec._open_qty.get(fill.symbol, Decimal("0"))
        new_qty = max(prev_qty - fill.qty, Decimal("0"))
        if new_qty <= Decimal("0"):
            rec._open_qty.pop(fill.symbol, None)
            rec._avg_cost.pop(fill.symbol, None)
        else:
            rec._open_qty[fill.symbol] = new_qty

    def _replay_consecutive_losses(self, agent_id: AgentId) -> int:
        """Replay fill history to compute consecutive_losses from scratch."""
        lots = sorted(
            [lot for lot in self._ledger.all_lots() if lot.agent_id == agent_id and lot.is_closed],
            key=lambda lot: lot.exit_date or date.min,
        )
        consecutive = 0
        for lot in lots:
            if lot.realized_pnl is not None:
                if lot.realized_pnl < Decimal("0"):
                    consecutive += 1
                else:
                    consecutive = 0
        return consecutive

    # ── SQLite persistence ────────────────────────────────────────────────────

    def _init_db(self) -> None:
        if self._db_path is None:
            return
        conn = sqlite3.connect(self._db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_tracker_state (
                agent_id          TEXT PRIMARY KEY,
                starting_equity   TEXT NOT NULL,
                current_equity    TEXT NOT NULL,
                peak_equity       TEXT NOT NULL,
                consecutive_losses INTEGER NOT NULL DEFAULT 0,
                is_benched        INTEGER NOT NULL DEFAULT 0,
                bench_until       TEXT,
                sizing_bucket     TEXT NOT NULL,
                recovery_target   TEXT,
                recovery_since    TEXT,
                recovery_days     INTEGER NOT NULL DEFAULT 0,
                last_update_date  TEXT
            )
        """)
        conn.commit()
        conn.close()

    def _load_or_init_records(self) -> None:
        if self._db_path is not None:
            loaded = self._load_from_db()
            if loaded:
                return
        # First run or no DB: initialise from scratch.
        for agent_id in AgentId:
            self._records[agent_id] = _PerAgentRecord(
                starting_equity=self._starting_equity,
                current_equity=self._starting_equity,
                peak_equity=self._starting_equity,
                consecutive_losses=0,
                is_benched=False,
                bench_until=None,
                sizing_bucket=DrawdownBucket.NORMAL,
                recovery_target=None,
                recovery_since=None,
                recovery_days=0,
                last_update_date=None,
            )

    def _load_from_db(self) -> bool:
        """Load records from DB.  Returns True if at least one row was found."""
        assert self._db_path is not None
        conn = sqlite3.connect(self._db_path)
        rows = conn.execute(
            "SELECT agent_id, starting_equity, current_equity, peak_equity, "
            "consecutive_losses, is_benched, bench_until, sizing_bucket, "
            "recovery_target, recovery_since, recovery_days, last_update_date "
            "FROM agent_tracker_state"
        ).fetchall()
        conn.close()
        if not rows:
            return False
        for row in rows:
            (
                agent_id_str,
                starting_equity,
                current_equity,
                peak_equity,
                consecutive_losses,
                is_benched,
                bench_until_str,
                sizing_bucket_str,
                recovery_target_str,
                recovery_since_str,
                recovery_days,
                last_update_date_str,
            ) = row
            try:
                agent_id = AgentId(agent_id_str)
            except ValueError:
                continue
            bench_until = (
                datetime.fromisoformat(bench_until_str) if bench_until_str else None
            )
            recovery_since = (
                date.fromisoformat(recovery_since_str) if recovery_since_str else None
            )
            recovery_target = (
                DrawdownBucket(recovery_target_str) if recovery_target_str else None
            )
            last_update_date = (
                date.fromisoformat(last_update_date_str) if last_update_date_str else None
            )
            self._records[agent_id] = _PerAgentRecord(
                starting_equity=Decimal(starting_equity),
                current_equity=Decimal(current_equity),
                peak_equity=Decimal(peak_equity),
                consecutive_losses=consecutive_losses,
                is_benched=bool(is_benched),
                bench_until=bench_until,
                sizing_bucket=DrawdownBucket(sizing_bucket_str),
                recovery_target=recovery_target,
                recovery_since=recovery_since,
                recovery_days=recovery_days,
                last_update_date=last_update_date,
            )
        # Fill any missing agents with defaults
        for agent_id in AgentId:
            if agent_id not in self._records:
                self._records[agent_id] = _PerAgentRecord(
                    starting_equity=self._starting_equity,
                    current_equity=self._starting_equity,
                    peak_equity=self._starting_equity,
                    consecutive_losses=0,
                    is_benched=False,
                    bench_until=None,
                    sizing_bucket=DrawdownBucket.NORMAL,
                    recovery_target=None,
                    recovery_since=None,
                    recovery_days=0,
                    last_update_date=None,
                )
        return True

    def _maybe_persist(self) -> None:
        """Persist current state to DB if a path is configured."""
        if self._db_path is None:
            return
        with self._lock:
            rows = []
            for agent_id, rec in self._records.items():
                rows.append((
                    agent_id,
                    str(rec.starting_equity),
                    str(rec.current_equity),
                    str(rec.peak_equity),
                    rec.consecutive_losses,
                    int(rec.is_benched),
                    rec.bench_until.isoformat() if rec.bench_until else None,
                    rec.sizing_bucket,
                    rec.recovery_target,
                    rec.recovery_since.isoformat() if rec.recovery_since else None,
                    rec.recovery_days,
                    rec.last_update_date.isoformat() if rec.last_update_date else None,
                ))
        conn = sqlite3.connect(self._db_path)
        conn.executemany(
            "INSERT OR REPLACE INTO agent_tracker_state "
            "(agent_id, starting_equity, current_equity, peak_equity, "
            "consecutive_losses, is_benched, bench_until, sizing_bucket, "
            "recovery_target, recovery_since, recovery_days, last_update_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
        conn.close()
