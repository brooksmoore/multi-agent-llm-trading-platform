"""Tests for execution/kill_switch.py — global halts and per-agent bench."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.types import AgentId, DrawdownBucket, KillSwitchState
from execution.kill_switch import (
    KillSwitchEngine,
    classify_drawdown,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


def _ts(offset_hours: float = 0.0) -> datetime:
    base = datetime(2026, 4, 24, 9, 30, tzinfo=UTC)
    return base + timedelta(hours=offset_hours)


# ── Initial state ─────────────────────────────────────────────────────────────


def test_initial_state_is_ok() -> None:
    eng = KillSwitchEngine()
    assert eng.state == KillSwitchState.OK


def test_initial_can_trade_and_open_new() -> None:
    eng = KillSwitchEngine()
    assert eng.can_trade() is True
    assert eng.can_open_new() is True


# ── Daily P&L ─────────────────────────────────────────────────────────────────


def test_daily_pnl_minus_2pct_trips_daily_loss() -> None:
    eng = KillSwitchEngine()
    result = eng.update_daily_pnl(Decimal("-0.02"))
    assert result == KillSwitchState.DAILY_LOSS
    assert eng.state == KillSwitchState.DAILY_LOSS


def test_daily_pnl_minus_199_does_not_trip() -> None:
    eng = KillSwitchEngine()
    result = eng.update_daily_pnl(Decimal("-0.0199"))
    assert result is None
    assert eng.state == KillSwitchState.OK


def test_daily_loss_blocks_new_entries() -> None:
    eng = KillSwitchEngine()
    eng.update_daily_pnl(Decimal("-0.03"))
    assert eng.can_open_new() is False
    assert eng.can_trade() is True  # existing trades still run


def test_reset_daily_clears_daily_loss() -> None:
    eng = KillSwitchEngine()
    eng.update_daily_pnl(Decimal("-0.05"))
    assert eng.state == KillSwitchState.DAILY_LOSS
    eng.reset_daily()
    assert eng.state == KillSwitchState.OK


# ── Drawdown ladder ───────────────────────────────────────────────────────────


def test_drawdown_15pct_trips_halved() -> None:
    eng = KillSwitchEngine()
    eng.update_nav(Decimal("100"))   # set peak
    result = eng.update_nav(Decimal("85"))  # -15%
    assert result == KillSwitchState.DRAWDOWN_HALVED
    assert eng.state == KillSwitchState.DRAWDOWN_HALVED


def test_drawdown_halved_allows_new_entries() -> None:
    """DRAWDOWN_HALVED halves sizing via the drawdown scalar; entries still allowed."""
    eng = KillSwitchEngine()
    eng.update_nav(Decimal("100"))
    eng.update_nav(Decimal("85"))
    assert eng.can_open_new() is True  # halved, not paused
    assert eng.can_trade() is True


def test_drawdown_25pct_trips_paused() -> None:
    eng = KillSwitchEngine()
    eng.update_nav(Decimal("100"))
    result = eng.update_nav(Decimal("75"))  # -25%
    assert result == KillSwitchState.DRAWDOWN_PAUSED
    assert eng.can_open_new() is False
    assert eng.can_trade() is True


def test_drawdown_33pct_trips_liquidate() -> None:
    eng = KillSwitchEngine()
    eng.update_nav(Decimal("100"))
    result = eng.update_nav(Decimal("67"))  # -33%
    assert result == KillSwitchState.DRAWDOWN_LIQUIDATE
    assert eng.can_trade() is False
    assert eng.can_open_new() is False


def test_drawdown_does_not_downgrade() -> None:
    """Once LIQUIDATE is tripped, a partial recovery doesn't drop back to PAUSED."""
    eng = KillSwitchEngine()
    eng.update_nav(Decimal("100"))
    eng.update_nav(Decimal("60"))  # -40% → LIQUIDATE
    assert eng.state == KillSwitchState.DRAWDOWN_LIQUIDATE
    eng.update_nav(Decimal("72"))  # now -28% → would be PAUSED, but should stay LIQUIDATE
    assert eng.state == KillSwitchState.DRAWDOWN_LIQUIDATE


def test_reset_daily_does_not_clear_drawdown() -> None:
    eng = KillSwitchEngine()
    eng.update_nav(Decimal("100"))
    eng.update_nav(Decimal("70"))  # -30% → PAUSED
    eng.reset_daily()
    assert eng.state == KillSwitchState.DRAWDOWN_PAUSED  # drawdown persists


def test_nav_advance_peak_correctly() -> None:
    eng = KillSwitchEngine()
    eng.update_nav(Decimal("100"))
    eng.update_nav(Decimal("110"))  # new peak
    eng.update_nav(Decimal("95"))   # -13.6% from 110 → below HALVED threshold
    assert eng.state == KillSwitchState.OK


# ── Heartbeat ─────────────────────────────────────────────────────────────────


def test_no_prior_heartbeat_is_not_overdue() -> None:
    eng = KillSwitchEngine(heartbeat_timeout_secs=60)
    assert eng.check_heartbeat(_ts()) is True
    assert eng.state == KillSwitchState.OK


def test_heartbeat_overdue_trips_missed() -> None:
    eng = KillSwitchEngine(heartbeat_timeout_secs=60)
    eng.record_heartbeat(_ts(0))
    # Check 2 minutes later (> 60s timeout)
    result = eng.check_heartbeat(_ts(2 / 60))
    assert result is False
    assert eng.state == KillSwitchState.HEARTBEAT_MISSED


def test_heartbeat_clears_missed() -> None:
    eng = KillSwitchEngine(heartbeat_timeout_secs=60)
    eng.record_heartbeat(_ts(0))
    eng.check_heartbeat(_ts(2 / 60))  # trip it
    assert eng.state == KillSwitchState.HEARTBEAT_MISSED
    eng.record_heartbeat(_ts(3 / 60))  # record new heartbeat → clears
    assert eng.state == KillSwitchState.OK


def test_heartbeat_within_timeout_does_not_trip() -> None:
    eng = KillSwitchEngine(heartbeat_timeout_secs=120)
    eng.record_heartbeat(_ts(0))
    result = eng.check_heartbeat(_ts(1 / 60))  # 1 minute later — within 2-min window
    assert result is True
    assert eng.state == KillSwitchState.OK


# ── Reconciliation break & budget ─────────────────────────────────────────────


def test_reconciliation_break_trips_and_clears() -> None:
    eng = KillSwitchEngine()
    eng.trip_reconciliation_break()
    assert eng.state == KillSwitchState.RECONCILIATION_BREAK
    assert eng.can_open_new() is False
    eng.clear_reconciliation_break()
    assert eng.state == KillSwitchState.OK


def test_budget_exhausted_trips() -> None:
    eng = KillSwitchEngine()
    eng.trip_budget_exhausted()
    assert eng.state == KillSwitchState.BUDGET_EXHAUSTED
    assert eng.can_open_new() is False


# ── Per-agent bench ───────────────────────────────────────────────────────────


def test_four_consecutive_losses_no_bench() -> None:
    eng = KillSwitchEngine()
    for _ in range(4):
        benched = eng.record_agent_result(AgentId.HAIKU, is_loss=True, ts=_ts())
    assert benched is False
    assert eng.is_agent_benched(AgentId.HAIKU, _ts()) is False


def test_five_consecutive_losses_benches_agent() -> None:
    eng = KillSwitchEngine()
    for _ in range(4):
        eng.record_agent_result(AgentId.HAIKU, is_loss=True, ts=_ts())
    benched = eng.record_agent_result(AgentId.HAIKU, is_loss=True, ts=_ts())
    assert benched is True
    assert eng.is_agent_benched(AgentId.HAIKU, _ts()) is True


def test_win_resets_consecutive_losses() -> None:
    eng = KillSwitchEngine()
    for _ in range(3):
        eng.record_agent_result(AgentId.HAIKU, is_loss=True, ts=_ts())
    eng.record_agent_result(AgentId.HAIKU, is_loss=False, ts=_ts())  # win
    assert eng.consecutive_losses(AgentId.HAIKU) == 0
    assert eng.is_agent_benched(AgentId.HAIKU, _ts()) is False


def test_bench_expires_after_24_hours() -> None:
    eng = KillSwitchEngine()
    bench_ts = _ts(0)
    for _ in range(5):
        eng.record_agent_result(AgentId.HAIKU, is_loss=True, ts=bench_ts)
    assert eng.is_agent_benched(AgentId.HAIKU, bench_ts) is True
    # 25h later → bench expired
    assert eng.is_agent_benched(AgentId.HAIKU, _ts(25)) is False


def test_bench_still_active_before_expiry() -> None:
    eng = KillSwitchEngine()
    bench_ts = _ts(0)
    for _ in range(5):
        eng.record_agent_result(AgentId.HAIKU, is_loss=True, ts=bench_ts)
    assert eng.is_agent_benched(AgentId.HAIKU, _ts(23)) is True


def test_bench_does_not_affect_other_agents() -> None:
    eng = KillSwitchEngine()
    for _ in range(5):
        eng.record_agent_result(AgentId.HAIKU, is_loss=True, ts=_ts())
    assert eng.is_agent_benched(AgentId.SONNET, _ts()) is False
    assert eng.is_agent_benched(AgentId.OPUS, _ts()) is False


# ── classify_drawdown ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pct", "expected"),
    [
        ("0.00", DrawdownBucket.NORMAL),
        ("0.049", DrawdownBucket.NORMAL),
        ("0.05", DrawdownBucket.YELLOW),
        ("0.099", DrawdownBucket.YELLOW),
        ("0.10", DrawdownBucket.ORANGE),
        ("0.149", DrawdownBucket.ORANGE),
        ("0.15", DrawdownBucket.RED),
        ("0.249", DrawdownBucket.RED),
        ("0.25", DrawdownBucket.FORCED_CASH),
        ("0.50", DrawdownBucket.FORCED_CASH),
    ],
)
def test_classify_drawdown(pct: str, expected: DrawdownBucket) -> None:
    assert classify_drawdown(Decimal(pct)) == expected
