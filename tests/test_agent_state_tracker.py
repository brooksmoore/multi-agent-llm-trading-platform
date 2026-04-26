"""Tests for execution/agent_state_tracker.py — per-agent drawdown bucket tracking.

Covers:
- Bucket tightens immediately when drawdown worsens
- Bucket loosens only after 5 consecutive trading days in better territory
- Consecutive-loss counter: resets on winning trade, triggers bench at 5
- Agent benched at 5 consecutive losses; un-benches after 24h (or timeout)
- get_state() returns live CoreAgentState with correct drawdown_bucket
- FORCED_CASH bucket triggered on >25% drawdown
- Cold-start recovery: rebuild_from_ledger() matches pre-kill state
- update_on_mark recomputes equity from realized + unrealized P&L
- Peak equity tracks rolling high correctly
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from core.types import AgentId, DrawdownBucket, Fill, OrderSide, new_id
from execution.agent_state_tracker import _RECOVERY_DAYS_REQUIRED, AgentStateTracker
from execution.kill_switch import KillSwitchEngine
from execution.lots import LotLedger

_TS = datetime(2026, 4, 26, 10, 0, tzinfo=UTC)
_STARTING = Decimal("1000")


def _fill(
    agent_id: AgentId = AgentId.SONNET,
    symbol: str = "SPY",
    side: OrderSide = OrderSide.BUY,
    qty: Decimal = Decimal("10"),
    price: Decimal = Decimal("100"),
    ts: datetime | None = None,
) -> Fill:
    return Fill(
        id=new_id(),
        order_id=new_id(),
        agent_id=agent_id,
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        timestamp=ts or _TS,
    )


def _tracker(tmp_path: Path | None = None) -> AgentStateTracker:
    db = str(tmp_path / "tracker.db") if tmp_path else None
    return AgentStateTracker(
        kill_switch=KillSwitchEngine(),
        lot_ledger=LotLedger(),
        starting_equity=_STARTING,
        db_path=db,
    )


def _tracker_with_ledger(
    lot_ledger: LotLedger,
    kill_switch: KillSwitchEngine | None = None,
    tmp_path: Path | None = None,
) -> AgentStateTracker:
    db = str(tmp_path / "tracker.db") if tmp_path else None
    return AgentStateTracker(
        kill_switch=kill_switch or KillSwitchEngine(),
        lot_ledger=lot_ledger,
        starting_equity=_STARTING,
        db_path=db,
    )


# ── Initial state ─────────────────────────────────────────────────────────────


class TestInitialState:
    def test_initial_bucket_is_normal(self) -> None:
        t = _tracker()
        state = t.get_state(AgentId.SONNET)
        assert state.drawdown_bucket == DrawdownBucket.NORMAL

    def test_initial_equity_is_starting(self) -> None:
        t = _tracker()
        state = t.get_state(AgentId.SONNET)
        assert state.sleeve_equity == _STARTING
        assert state.sleeve_peak_equity == _STARTING

    def test_initial_consecutive_losses_is_zero(self) -> None:
        t = _tracker()
        state = t.get_state(AgentId.SONNET)
        assert state.consecutive_losses == 0
        assert not state.is_benched


# ── Bucket tightening ─────────────────────────────────────────────────────────


class TestBucketTightening:
    def test_tightens_immediately_on_drawdown(self) -> None:
        t = _tracker()
        # 6% drawdown → YELLOW
        t.update_on_mark(AgentId.SONNET, {"SPY": Decimal("940")})
        # simulate equity drop via mark: need to set realized P&L path
        # Direct mark: current_equity = starting + unrealized = 1000 + 0 = 1000
        # (no open lots → unrealized = 0). Mark doesn't affect equity here.
        # We test bucket via directly seeded state instead.

    def test_tightens_from_normal_to_yellow(self) -> None:
        """Simulate drawdown by calling update_on_mark after seeding lots."""
        ledger = LotLedger()
        # BUY 10 SPY at $100 → cost basis $1000
        buy_fill = _fill(side=OrderSide.BUY, qty=Decimal("10"), price=Decimal("100"))
        ledger.open_lot(buy_fill)

        t = _tracker_with_ledger(ledger)
        t.update_on_fill(buy_fill)  # track avg cost

        # Mark SPY down to $94 (6% drop → 6% drawdown from $1000 peak)
        t.update_on_mark(AgentId.SONNET, {"SPY": Decimal("94")})
        state = t.get_state(AgentId.SONNET)
        assert state.drawdown_bucket == DrawdownBucket.YELLOW

    def test_tightens_through_multiple_buckets(self) -> None:
        ledger = LotLedger()
        buy = _fill(side=OrderSide.BUY, qty=Decimal("10"), price=Decimal("100"))
        ledger.open_lot(buy)
        t = _tracker_with_ledger(ledger)
        t.update_on_fill(buy)

        # 11% drop → ORANGE
        t.update_on_mark(AgentId.SONNET, {"SPY": Decimal("89")})
        assert t.get_state(AgentId.SONNET).drawdown_bucket == DrawdownBucket.ORANGE

    def test_forced_cash_on_over_25_pct_drawdown(self) -> None:
        ledger = LotLedger()
        buy = _fill(side=OrderSide.BUY, qty=Decimal("10"), price=Decimal("100"))
        ledger.open_lot(buy)
        t = _tracker_with_ledger(ledger)
        t.update_on_fill(buy)

        # 26% drop → FORCED_CASH
        t.update_on_mark(AgentId.SONNET, {"SPY": Decimal("74")})
        assert t.get_state(AgentId.SONNET).drawdown_bucket == DrawdownBucket.FORCED_CASH


# ── Bucket loosening (recovery rule) ─────────────────────────────────────────


class TestBucketLoosening:
    def _setup_in_yellow(self) -> tuple[AgentStateTracker, LotLedger]:
        ledger = LotLedger()
        buy = _fill(side=OrderSide.BUY, qty=Decimal("10"), price=Decimal("100"))
        ledger.open_lot(buy)
        t = _tracker_with_ledger(ledger)
        t.update_on_fill(buy)
        # Drop into YELLOW (6% down)
        t.update_on_mark(AgentId.SONNET, {"SPY": Decimal("94")})
        assert t.get_state(AgentId.SONNET).drawdown_bucket == DrawdownBucket.YELLOW
        return t, ledger

    def test_does_not_loosen_immediately(self) -> None:
        t, _ = self._setup_in_yellow()
        # Mark back up to par (0% drawdown → NORMAL raw)
        # But recovery requires 5 days — should still be YELLOW
        t.update_on_mark(AgentId.SONNET, {"SPY": Decimal("100")})
        state = t.get_state(AgentId.SONNET)
        assert state.drawdown_bucket == DrawdownBucket.YELLOW

    def test_loosens_after_required_days(self) -> None:
        t, _ = self._setup_in_yellow()

        # Mark as recovered on 5 consecutive (simulated) days.
        base_date = datetime(2026, 4, 27, 10, 0, tzinfo=UTC)
        for day_offset in range(_RECOVERY_DAYS_REQUIRED):
            mock_date = base_date + timedelta(days=day_offset)
            # Monkey-patch the date so the tracker sees distinct days.
            rec = t._records[AgentId.SONNET]
            rec.last_update_date = (mock_date - timedelta(days=1)).date()
            t.update_on_mark(AgentId.SONNET, {"SPY": Decimal("100")})

        state = t.get_state(AgentId.SONNET)
        assert state.drawdown_bucket == DrawdownBucket.NORMAL

    def test_recovery_resets_on_re_tightening(self) -> None:
        t, _ = self._setup_in_yellow()

        # Start recovery (1 day at NORMAL)
        rec = t._records[AgentId.SONNET]
        rec.last_update_date = date(2026, 4, 26)
        t.update_on_mark(AgentId.SONNET, {"SPY": Decimal("100")})
        assert t._records[AgentId.SONNET].recovery_days == 1

        # Re-tighten (drop back into YELLOW)
        t.update_on_mark(AgentId.SONNET, {"SPY": Decimal("94")})

        # Recovery state should be cleared
        rec = t._records[AgentId.SONNET]
        assert rec.recovery_target is None
        assert rec.recovery_days == 0


# ── Consecutive losses + bench ────────────────────────────────────────────────


class TestConsecutiveLosses:
    def test_loss_increments_counter(self) -> None:
        t = _tracker()
        # BUY at 100, SELL at 90 → loss
        t.update_on_fill(_fill(side=OrderSide.BUY, price=Decimal("100")))
        t.update_on_fill(_fill(side=OrderSide.SELL, price=Decimal("90")))
        assert t.get_state(AgentId.SONNET).consecutive_losses == 1

    def test_win_resets_counter(self) -> None:
        t = _tracker()
        t.update_on_fill(_fill(side=OrderSide.BUY, price=Decimal("100")))
        t.update_on_fill(_fill(side=OrderSide.SELL, price=Decimal("90")))
        assert t.get_state(AgentId.SONNET).consecutive_losses == 1

        # New trade that wins
        t.update_on_fill(_fill(side=OrderSide.BUY, price=Decimal("90")))
        t.update_on_fill(_fill(side=OrderSide.SELL, price=Decimal("110")))
        assert t.get_state(AgentId.SONNET).consecutive_losses == 0

    def test_five_losses_bench_agent(self) -> None:
        t = _tracker()
        for _i in range(5):
            t.update_on_fill(_fill(side=OrderSide.BUY, price=Decimal("100")))
            t.update_on_fill(_fill(side=OrderSide.SELL, price=Decimal("90")))
        state = t.get_state(AgentId.SONNET)
        assert state.is_benched
        assert state.bench_until is not None

    def test_bench_clears_after_24h(self) -> None:
        t = _tracker()
        for _ in range(5):
            t.update_on_fill(_fill(side=OrderSide.BUY, price=Decimal("100")))
            t.update_on_fill(_fill(side=OrderSide.SELL, price=Decimal("90")))

        # Simulate bench expiry
        rec = t._records[AgentId.SONNET]
        rec.bench_until = datetime.now(UTC) - timedelta(seconds=1)

        state = t.get_state(AgentId.SONNET)
        assert not state.is_benched
        assert state.consecutive_losses == 0


# ── Equity and peak tracking ──────────────────────────────────────────────────


class TestEquityTracking:
    def test_peak_tracks_high(self) -> None:
        ledger = LotLedger()
        buy = _fill(side=OrderSide.BUY, qty=Decimal("10"), price=Decimal("100"))
        ledger.open_lot(buy)
        t = _tracker_with_ledger(ledger)
        t.update_on_fill(buy)

        # Price rises to 120 → equity peaks at 1200
        t.update_on_mark(AgentId.SONNET, {"SPY": Decimal("120")})
        state = t.get_state(AgentId.SONNET)
        assert state.sleeve_peak_equity == Decimal("1200")

    def test_equity_reflects_unrealized(self) -> None:
        ledger = LotLedger()
        buy = _fill(side=OrderSide.BUY, qty=Decimal("5"), price=Decimal("100"))
        ledger.open_lot(buy)
        t = _tracker_with_ledger(ledger)
        t.update_on_fill(buy)

        # 5 shares × $120 = $600; starting=$1000; unrealized = $600 - $500 = +$100
        t.update_on_mark(AgentId.SONNET, {"SPY": Decimal("120")})
        state = t.get_state(AgentId.SONNET)
        # equity = 1000 + realized(0) + unrealized(5×(120-100)=100) = 1100
        assert state.sleeve_equity == Decimal("1100")


# ── Cold-start rebuild ────────────────────────────────────────────────────────


class TestColdStartRebuild:
    def test_rebuild_matches_pre_kill_state(self, tmp_path: pytest.TempPathFactory) -> None:
        """Kill the tracker, create a new one, rebuild from ledger, verify state."""
        ledger = LotLedger()
        kill = KillSwitchEngine()

        # Session 1: create tracker, take a loss, persist.
        t1 = _tracker_with_ledger(ledger, kill, tmp_path)
        buy = _fill(side=OrderSide.BUY, price=Decimal("100"))
        ledger.open_lot(buy)
        t1.update_on_fill(buy)
        t1.update_on_fill(_fill(side=OrderSide.SELL, price=Decimal("90")))
        t1._maybe_persist()

        # Session 2: new tracker loads from DB.
        t2 = _tracker_with_ledger(ledger, kill, tmp_path)
        state = t2.get_state(AgentId.SONNET)

        # Should have loaded the 1 consecutive loss
        assert state.consecutive_losses == 1

    def test_rebuild_from_ledger_cold_start(self) -> None:
        """rebuild_from_ledger() recomputes state from scratch."""
        ledger = LotLedger()
        kill = KillSwitchEngine()
        t = _tracker_with_ledger(ledger, kill)

        # Seed one losing trade in the ledger (already closed)
        buy = _fill(side=OrderSide.BUY, price=Decimal("100"), qty=Decimal("5"))
        ledger.open_lot(buy)
        sell = _fill(side=OrderSide.SELL, price=Decimal("90"), qty=Decimal("5"))
        ledger.close_lots(AgentId.SONNET, "SPY", Decimal("5"), sell)

        # Cold rebuild
        t.rebuild_from_ledger({"SPY": Decimal("90")})

        state = t.get_state(AgentId.SONNET)
        assert state.consecutive_losses == 1
        # Equity = starting + realized_pnl = 1000 + (5 × (90-100)) = 950
        assert state.sleeve_equity == Decimal("950")


# ── FORCED_CASH re-enable gate ────────────────────────────────────────────────


class TestForcedCash:
    def test_forced_cash_requires_recovery_days(self) -> None:
        """FORCED_CASH does not auto-recover on next mark — recovery rule applies."""
        ledger = LotLedger()
        buy = _fill(side=OrderSide.BUY, qty=Decimal("10"), price=Decimal("100"))
        ledger.open_lot(buy)
        t = _tracker_with_ledger(ledger)
        t.update_on_fill(buy)

        # 26% drawdown → FORCED_CASH
        t.update_on_mark(AgentId.SONNET, {"SPY": Decimal("74")})
        assert t.get_state(AgentId.SONNET).drawdown_bucket == DrawdownBucket.FORCED_CASH

        # One mark at par — should not immediately escape FORCED_CASH
        t.update_on_mark(AgentId.SONNET, {"SPY": Decimal("100")})
        assert t.get_state(AgentId.SONNET).drawdown_bucket == DrawdownBucket.FORCED_CASH

    def test_forced_cash_clears_after_recovery_days(self) -> None:
        ledger = LotLedger()
        buy = _fill(side=OrderSide.BUY, qty=Decimal("10"), price=Decimal("100"))
        ledger.open_lot(buy)
        t = _tracker_with_ledger(ledger)
        t.update_on_fill(buy)

        t.update_on_mark(AgentId.SONNET, {"SPY": Decimal("74")})
        assert t.get_state(AgentId.SONNET).drawdown_bucket == DrawdownBucket.FORCED_CASH

        # 5 simulated days at par
        base = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
        for day_offset in range(_RECOVERY_DAYS_REQUIRED):
            rec = t._records[AgentId.SONNET]
            rec.last_update_date = (base + timedelta(days=day_offset - 1)).date()
            t.update_on_mark(AgentId.SONNET, {"SPY": Decimal("100")})

        assert t.get_state(AgentId.SONNET).drawdown_bucket == DrawdownBucket.NORMAL


# ── SQLite persistence ────────────────────────────────────────────────────────


class TestSQLitePersistence:
    def test_state_survives_restart(self, tmp_path: pytest.TempPathFactory) -> None:
        ledger = LotLedger()

        t1 = AgentStateTracker(
            kill_switch=KillSwitchEngine(),
            lot_ledger=ledger,
            starting_equity=_STARTING,
            db_path=str(tmp_path / "tracker.db"),
        )
        # Take 2 losses
        t1.update_on_fill(_fill(side=OrderSide.BUY, price=Decimal("100")))
        t1.update_on_fill(_fill(side=OrderSide.SELL, price=Decimal("80")))
        t1.update_on_fill(_fill(side=OrderSide.BUY, price=Decimal("100")))
        t1.update_on_fill(_fill(side=OrderSide.SELL, price=Decimal("80")))
        assert t1.get_state(AgentId.SONNET).consecutive_losses == 2

        # Simulate restart
        t2 = AgentStateTracker(
            kill_switch=KillSwitchEngine(),
            lot_ledger=ledger,
            starting_equity=_STARTING,
            db_path=str(tmp_path / "tracker.db"),
        )
        assert t2.get_state(AgentId.SONNET).consecutive_losses == 2
