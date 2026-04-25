"""Kill switch engine: global portfolio halts and per-agent bench logic.

Global thresholds (portfolio drawdown from rolling peak):
    -2%  intraday P&L  → DAILY_LOSS   (no new entries; sizes already submitted run)
    -15% drawdown      → DRAWDOWN_HALVED  (new entries still allowed; sizing cut by ladder)
    -25% drawdown      → DRAWDOWN_PAUSED  (no new entries; only closes)
    -33% drawdown      → DRAWDOWN_LIQUIDATE (liquidate all; only closes)

Per-agent: 5 consecutive losing intents → 24-hour bench.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.types import AgentId, DrawdownBucket, KillSwitchState

# ── Global thresholds (positive magnitudes) ───────────────────────────────────

DAILY_LOSS_THRESHOLD: Decimal = Decimal("0.02")         # -2% intraday
DRAWDOWN_HALVED_THRESHOLD: Decimal = Decimal("0.15")    # -15% from peak
DRAWDOWN_PAUSED_THRESHOLD: Decimal = Decimal("0.25")    # -25% from peak
DRAWDOWN_LIQUIDATE_THRESHOLD: Decimal = Decimal("0.33") # -33% from peak

# ── Per-agent bench ───────────────────────────────────────────────────────────

CONSECUTIVE_LOSS_BENCH_TRIGGER: int = 5
BENCH_DURATION_HOURS: int = 24


@dataclass
class _AgentRecord:
    consecutive_losses: int = 0
    benched_until: datetime | None = None


@dataclass
class KillSwitchSnapshot:
    """Read-only snapshot of global kill switch state for dashboards/tests."""

    state: KillSwitchState
    daily_pnl_pct: Decimal
    peak_nav: Decimal | None
    current_nav: Decimal | None
    last_heartbeat: datetime | None


class KillSwitchEngine:
    """Thread-safe global kill switch.

    Tracks intraday P&L, portfolio drawdown from rolling peak, heartbeat
    liveness, reconciliation breaks, and budget exhaustion.

    Call `reset_daily()` at market open each day to clear DAILY_LOSS state.
    """

    def __init__(self, heartbeat_timeout_secs: int = 120) -> None:
        self._lock = threading.Lock()
        self._state: KillSwitchState = KillSwitchState.OK
        self._daily_pnl_pct: Decimal = Decimal("0")
        self._peak_nav: Decimal | None = None
        self._current_nav: Decimal | None = None
        self._last_heartbeat: datetime | None = None
        self._heartbeat_timeout_secs: int = heartbeat_timeout_secs
        self._agent_records: dict[AgentId, _AgentRecord] = {
            aid: _AgentRecord() for aid in AgentId
        }

    # ── State inspection ──────────────────────────────────────────────────────

    @property
    def state(self) -> KillSwitchState:
        with self._lock:
            return self._state

    def can_trade(self) -> bool:
        """False only when LIQUIDATE — all positions must be closed."""
        return self.state != KillSwitchState.DRAWDOWN_LIQUIDATE

    def can_open_new(self) -> bool:
        """False for any state that blocks opening new positions."""
        return self.state not in _BLOCKS_NEW_ENTRIES

    def portfolio_drawdown_pct(self) -> Decimal:
        """Current drawdown from rolling peak as a positive fraction (0.15 = 15%)."""
        with self._lock:
            return self._compute_drawdown()

    def snapshot(self) -> KillSwitchSnapshot:
        with self._lock:
            return KillSwitchSnapshot(
                state=self._state,
                daily_pnl_pct=self._daily_pnl_pct,
                peak_nav=self._peak_nav,
                current_nav=self._current_nav,
                last_heartbeat=self._last_heartbeat,
            )

    # ── Updates ───────────────────────────────────────────────────────────────

    def update_daily_pnl(self, pnl_pct: Decimal) -> KillSwitchState | None:
        """Update intraday P&L (negative = loss). Returns new state if tripped."""
        with self._lock:
            self._daily_pnl_pct = pnl_pct
            if pnl_pct <= -DAILY_LOSS_THRESHOLD and self._state == KillSwitchState.OK:
                self._state = KillSwitchState.DAILY_LOSS
                return self._state
        return None

    def update_nav(self, nav: Decimal) -> KillSwitchState | None:
        """Update current NAV; advance rolling peak; trip drawdown switches if needed."""
        with self._lock:
            if self._peak_nav is None or nav > self._peak_nav:
                self._peak_nav = nav
            self._current_nav = nav
            dd = self._compute_drawdown()
            return self._apply_drawdown(dd)

    def record_heartbeat(self, ts: datetime) -> None:
        """Record a heartbeat. Clears HEARTBEAT_MISSED if currently set."""
        with self._lock:
            self._last_heartbeat = ts
            if self._state == KillSwitchState.HEARTBEAT_MISSED:
                self._state = KillSwitchState.OK

    def check_heartbeat(self, ts: datetime) -> bool:
        """Return False (and trip HEARTBEAT_MISSED) if heartbeat is overdue."""
        with self._lock:
            if self._last_heartbeat is None:
                return True  # no prior heartbeat yet; not yet overdue
            elapsed = (ts - self._last_heartbeat).total_seconds()
            if elapsed > self._heartbeat_timeout_secs:
                self._state = KillSwitchState.HEARTBEAT_MISSED
                return False
            return True

    def trip_reconciliation_break(self) -> KillSwitchState:
        with self._lock:
            self._state = KillSwitchState.RECONCILIATION_BREAK
            return self._state

    def trip_budget_exhausted(self) -> KillSwitchState:
        with self._lock:
            self._state = KillSwitchState.BUDGET_EXHAUSTED
            return self._state

    def reset_daily(self) -> None:
        """Call at market open. Clears DAILY_LOSS; does NOT clear drawdown states."""
        with self._lock:
            self._daily_pnl_pct = Decimal("0")
            if self._state == KillSwitchState.DAILY_LOSS:
                self._state = KillSwitchState.OK

    def clear_reconciliation_break(self) -> None:
        with self._lock:
            if self._state == KillSwitchState.RECONCILIATION_BREAK:
                self._state = KillSwitchState.OK

    # ── Per-agent bench ───────────────────────────────────────────────────────

    def record_agent_result(
        self, agent_id: AgentId, is_loss: bool, ts: datetime
    ) -> bool:
        """Record a win or loss. Returns True if the agent was just benched."""
        with self._lock:
            rec = self._agent_records[agent_id]
            if is_loss:
                rec.consecutive_losses += 1
                if rec.consecutive_losses >= CONSECUTIVE_LOSS_BENCH_TRIGGER:
                    rec.benched_until = ts + timedelta(hours=BENCH_DURATION_HOURS)
                    return True
            else:
                rec.consecutive_losses = 0
                rec.benched_until = None
        return False

    def is_agent_benched(self, agent_id: AgentId, ts: datetime) -> bool:
        with self._lock:
            rec = self._agent_records[agent_id]
            if rec.benched_until is None:
                return False
            if ts < rec.benched_until:
                return True
            # Bench expired; clear it.
            rec.benched_until = None
            rec.consecutive_losses = 0
            return False

    def consecutive_losses(self, agent_id: AgentId) -> int:
        with self._lock:
            return self._agent_records[agent_id].consecutive_losses

    # ── Internals ─────────────────────────────────────────────────────────────

    def _compute_drawdown(self) -> Decimal:
        """Positive fraction drawdown from peak. Caller must hold self._lock."""
        if self._peak_nav is None or self._current_nav is None:
            return Decimal("0")
        if self._peak_nav == Decimal("0"):
            return Decimal("0")
        return (self._peak_nav - self._current_nav) / self._peak_nav

    def _apply_drawdown(self, dd: Decimal) -> KillSwitchState | None:
        """Trip the appropriate state for current drawdown. Caller holds lock."""
        old = self._state
        if dd >= DRAWDOWN_LIQUIDATE_THRESHOLD:
            self._state = KillSwitchState.DRAWDOWN_LIQUIDATE
        elif dd >= DRAWDOWN_PAUSED_THRESHOLD:
            if self._state not in (KillSwitchState.DRAWDOWN_LIQUIDATE,):
                self._state = KillSwitchState.DRAWDOWN_PAUSED
        elif dd >= DRAWDOWN_HALVED_THRESHOLD and self._state not in _DRAWDOWN_WORSE_STATES:
            self._state = KillSwitchState.DRAWDOWN_HALVED
        if self._state != old:
            return self._state
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────


def classify_drawdown(drawdown_pct: Decimal) -> DrawdownBucket:
    """Map a positive drawdown fraction to a DrawdownBucket (for leverage scalar).

    Uses per-agent sleeve drawdown, NOT global portfolio drawdown.
    """
    if drawdown_pct >= Decimal("0.25"):
        return DrawdownBucket.FORCED_CASH
    if drawdown_pct >= Decimal("0.15"):
        return DrawdownBucket.RED
    if drawdown_pct >= Decimal("0.10"):
        return DrawdownBucket.ORANGE
    if drawdown_pct >= Decimal("0.05"):
        return DrawdownBucket.YELLOW
    return DrawdownBucket.NORMAL


# States that prevent opening new positions
_BLOCKS_NEW_ENTRIES: frozenset[KillSwitchState] = frozenset({
    KillSwitchState.DRAWDOWN_PAUSED,
    KillSwitchState.DRAWDOWN_LIQUIDATE,
    KillSwitchState.DAILY_LOSS,
    KillSwitchState.HEARTBEAT_MISSED,
    KillSwitchState.RECONCILIATION_BREAK,
    KillSwitchState.BUDGET_EXHAUSTED,
})

# Drawdown states that are "worse" than DRAWDOWN_HALVED (don't downgrade)
_DRAWDOWN_WORSE_STATES: frozenset[KillSwitchState] = frozenset({
    KillSwitchState.DRAWDOWN_PAUSED,
    KillSwitchState.DRAWDOWN_LIQUIDATE,
})


def utcnow() -> datetime:
    return datetime.now(UTC)
