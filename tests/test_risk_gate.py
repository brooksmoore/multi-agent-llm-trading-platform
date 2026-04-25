"""Tests for execution/risk.py — pre-trade RiskGate checks."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from core.types import (
    Action,
    AgentId,
    AgentState,
    AssetClass,
    DrawdownBucket,
    Intent,
    KillSwitchState,
    OrderSide,
    Position,
    Sleeve,
    new_id,
)
from execution.kill_switch import KillSwitchEngine
from execution.lots import LotLedger
from execution.risk import AGENT_SINGLE_NAME_CAPS, RiskGate
from execution.tax import WashSaleChecker

# ── Helpers ───────────────────────────────────────────────────────────────────

_TS = datetime(2026, 4, 24, 10, 0, tzinfo=UTC)
_TODAY = _TS.date()


def _gate() -> tuple[RiskGate, KillSwitchEngine, LotLedger]:
    kill = KillSwitchEngine()
    wash = WashSaleChecker()
    lots = LotLedger()
    gate = RiskGate(kill, wash, lots)
    return gate, kill, lots


def _agent_state(
    agent: AgentId = AgentId.HAIKU,
    dd_bucket: DrawdownBucket = DrawdownBucket.NORMAL,
    sleeve_equity: str = "1000",
) -> AgentState:
    return AgentState(
        agent_id=agent,
        sleeve_equity=Decimal(sleeve_equity),
        sleeve_peak_equity=Decimal(sleeve_equity),
        drawdown_bucket=dd_bucket,
        drawdown_bucket_entry_date=None,
        consecutive_losses=0,
        is_benched=False,
        bench_until=None,
        day_trade_count=0,
        orders_today=0,
        last_memo_id=None,
    )


def _intent(
    agent: AgentId = AgentId.HAIKU,
    symbol: str = "SPY",
    action: Action = Action.BUY,
    weight: str = "0.10",
    sleeve: Sleeve = Sleeve.EQUITY,
) -> Intent:
    return Intent(
        id=new_id(),
        agent_id=agent,
        symbol=symbol,
        action=action,
        target_weight=Decimal(weight),
        sleeve=sleeve,
        signal="test signal",
        conviction=7,
        rationale="test rationale",
        timestamp=_TS,
    )


def _position(
    agent: AgentId = AgentId.HAIKU,
    symbol: str = "SPY",
    qty: str = "10",
    price: str = "100",
    sleeve: Sleeve = Sleeve.EQUITY,
) -> Position:
    return Position(
        agent_id=agent,
        symbol=symbol,
        qty=Decimal(qty),
        avg_entry_price=Decimal(price),
        current_price=Decimal(price),
        asset_class=AssetClass.ETF,
        sleeve=sleeve,
        as_of=_TS,
    )


# ── Happy path ────────────────────────────────────────────────────────────────


def test_clean_intent_is_allowed() -> None:
    gate, _, _ = _gate()
    decision = gate.check_intent(
        _intent(), _agent_state(), Decimal("1.0"), [], _TS
    )
    assert decision.allowed is True
    assert decision.veto_reason is None
    assert decision.capped_weight is None


# ── Kill switch checks ────────────────────────────────────────────────────────


def test_liquidate_blocks_buy() -> None:
    gate, kill, _ = _gate()
    kill.update_nav(Decimal("100"))
    kill.update_nav(Decimal("60"))  # -40% → LIQUIDATE
    assert kill.state == KillSwitchState.DRAWDOWN_LIQUIDATE

    decision = gate.check_intent(
        _intent(action=Action.BUY), _agent_state(), Decimal("1.0"), [], _TS
    )
    assert decision.allowed is False
    assert "drawdown_liquidate" in (decision.veto_reason or "")


def test_liquidate_allows_sell() -> None:
    gate, kill, _ = _gate()
    kill.update_nav(Decimal("100"))
    kill.update_nav(Decimal("60"))
    decision = gate.check_intent(
        _intent(action=Action.SELL), _agent_state(), Decimal("1.0"), [], _TS
    )
    assert decision.allowed is True


def test_paused_blocks_buy() -> None:
    gate, kill, _ = _gate()
    kill.update_nav(Decimal("100"))
    kill.update_nav(Decimal("74"))  # -26% → PAUSED
    decision = gate.check_intent(
        _intent(action=Action.BUY), _agent_state(), Decimal("1.0"), [], _TS
    )
    assert decision.allowed is False
    assert "drawdown_paused" in (decision.veto_reason or "")


def test_paused_allows_close() -> None:
    gate, kill, _ = _gate()
    kill.update_nav(Decimal("100"))
    kill.update_nav(Decimal("74"))
    decision = gate.check_intent(
        _intent(action=Action.CLOSE), _agent_state(), Decimal("1.0"), [], _TS
    )
    assert decision.allowed is True


def test_daily_loss_blocks_new_entry() -> None:
    gate, kill, _ = _gate()
    kill.update_daily_pnl(Decimal("-0.03"))
    decision = gate.check_intent(
        _intent(action=Action.BUY), _agent_state(), Decimal("1.0"), [], _TS
    )
    assert decision.allowed is False
    assert "daily_loss" in (decision.veto_reason or "")


def test_halved_allows_new_entry() -> None:
    """DRAWDOWN_HALVED (-15%) still allows new entries; sizing is cut by the ladder."""
    gate, kill, _ = _gate()
    kill.update_nav(Decimal("100"))
    kill.update_nav(Decimal("84"))  # -16% → HALVED
    assert kill.state == KillSwitchState.DRAWDOWN_HALVED
    decision = gate.check_intent(
        _intent(action=Action.BUY), _agent_state(), Decimal("0.5"), [], _TS
    )
    assert decision.allowed is True


# ── Agent bench ───────────────────────────────────────────────────────────────


def test_benched_agent_is_rejected() -> None:
    gate, kill, _ = _gate()
    for _ in range(5):
        kill.record_agent_result(AgentId.HAIKU, is_loss=True, ts=_TS)
    decision = gate.check_intent(_intent(), _agent_state(), Decimal("1.0"), [], _TS)
    assert decision.allowed is False
    assert "benched" in (decision.veto_reason or "")


# ── FORCED_CASH drawdown bucket ───────────────────────────────────────────────


def test_forced_cash_bucket_blocks_buy() -> None:
    gate, _, _ = _gate()
    state = _agent_state(dd_bucket=DrawdownBucket.FORCED_CASH)
    decision = gate.check_intent(_intent(action=Action.BUY), state, Decimal("1.0"), [], _TS)
    assert decision.allowed is False
    assert "FORCED_CASH" in (decision.veto_reason or "")


def test_forced_cash_bucket_allows_sell() -> None:
    gate, _, _ = _gate()
    state = _agent_state(dd_bucket=DrawdownBucket.FORCED_CASH)
    decision = gate.check_intent(_intent(action=Action.SELL), state, Decimal("1.0"), [], _TS)
    assert decision.allowed is True


# ── LETF checks ───────────────────────────────────────────────────────────────


def test_letf_whitelist_buy_allowed_when_fresh() -> None:
    gate, _, _ = _gate()
    decision = gate.check_intent(
        _intent(symbol="TQQQ"), _agent_state(), Decimal("1.0"), [], _TS
    )
    assert decision.allowed is True


def test_letf_existing_overdue_position_blocks_buy() -> None:
    gate, _, lots = _gate()
    # Open a lot 10 days ago (> 5-day limit)
    from core.types import Fill
    old_ts = datetime(2026, 4, 14, 10, 0, tzinfo=UTC)  # 10 days ago
    buy_fill = Fill(
        id=new_id(),
        order_id=new_id(),
        agent_id=AgentId.HAIKU,
        symbol="TQQQ",
        side=OrderSide.BUY,
        qty=Decimal("10"),
        price=Decimal("50"),
        timestamp=old_ts,
    )
    lots.open_lot(buy_fill)

    decision = gate.check_intent(
        _intent(symbol="TQQQ"), _agent_state(), Decimal("1.0"), [], _TS
    )
    assert decision.allowed is False
    assert "letf" in (decision.veto_reason or "").lower()


def test_letf_sell_always_allowed_even_overdue() -> None:
    gate, _, lots = _gate()
    from core.types import Fill
    old_ts = datetime(2026, 4, 14, 10, 0, tzinfo=UTC)
    buy_fill = Fill(
        id=new_id(), order_id=new_id(), agent_id=AgentId.HAIKU, symbol="TQQQ",
        side=OrderSide.BUY, qty=Decimal("10"), price=Decimal("50"), timestamp=old_ts,
    )
    lots.open_lot(buy_fill)

    decision = gate.check_intent(
        _intent(symbol="TQQQ", action=Action.SELL), _agent_state(), Decimal("1.0"), [], _TS
    )
    assert decision.allowed is True


def test_non_letf_symbol_passes_without_hold_check() -> None:
    gate, _, _ = _gate()
    decision = gate.check_intent(
        _intent(symbol="AAPL"), _agent_state(), Decimal("1.0"), [], _TS
    )
    assert decision.allowed is True


# ── Options cap ───────────────────────────────────────────────────────────────


def test_options_within_20pct_allowed() -> None:
    gate, _, _ = _gate()
    intent = _intent(symbol="AAPL_OPT", weight="0.10", sleeve=Sleeve.OPTIONS)
    state = _agent_state(sleeve_equity="1000")
    decision = gate.check_intent(intent, state, Decimal("1.0"), [], _TS)
    assert decision.allowed is True


def test_options_exceeding_20pct_blocked() -> None:
    gate, _, _ = _gate()
    # Existing options position at 15% of sleeve (150/1000)
    opts_pos = _position(symbol="SPY_OPT", qty="1", price="150", sleeve=Sleeve.OPTIONS)
    # New intent for 10% → total = 25% > 20%
    intent = _intent(symbol="QQQ_OPT", weight="0.10", sleeve=Sleeve.OPTIONS)
    state = _agent_state(sleeve_equity="1000")
    decision = gate.check_intent(intent, state, Decimal("1.0"), [opts_pos], _TS)
    assert decision.allowed is False
    assert "options" in (decision.veto_reason or "").lower()


# ── Single-name weight cap ────────────────────────────────────────────────────


def test_weight_within_cap_passes() -> None:
    gate, _, _ = _gate()
    decision = gate.check_intent(
        _intent(agent=AgentId.SONNET, symbol="NVDA", weight="0.12"),
        _agent_state(AgentId.SONNET),
        Decimal("1.0"),
        [],
        _TS,
    )
    assert decision.allowed is True
    assert decision.capped_weight is None


def test_weight_exceeding_cap_is_capped() -> None:
    gate, _, _ = _gate()
    # Sonnet cap is 12%; intent at 15%
    decision = gate.check_intent(
        _intent(agent=AgentId.SONNET, symbol="NVDA", weight="0.15"),
        _agent_state(AgentId.SONNET),
        Decimal("1.0"),
        [],
        _TS,
    )
    assert decision.allowed is True  # capped, not vetoed
    assert decision.capped_weight == AGENT_SINGLE_NAME_CAPS[AgentId.SONNET]


def test_haiku_etf_cap_25pct() -> None:
    gate, _, _ = _gate()
    decision = gate.check_intent(
        _intent(agent=AgentId.HAIKU, symbol="SPY", weight="0.30"),
        _agent_state(AgentId.HAIKU),
        Decimal("1.0"),
        [],
        _TS,
    )
    assert decision.allowed is True
    assert decision.capped_weight == AGENT_SINGLE_NAME_CAPS[AgentId.HAIKU]


# ── effective_gross == 0 ──────────────────────────────────────────────────────


def test_zero_effective_gross_blocks_buy() -> None:
    gate, _, _ = _gate()
    decision = gate.check_intent(
        _intent(action=Action.BUY), _agent_state(), Decimal("0"), [], _TS
    )
    assert decision.allowed is False
    assert "effective_gross=0" in (decision.veto_reason or "")


def test_zero_effective_gross_allows_sell() -> None:
    gate, _, _ = _gate()
    decision = gate.check_intent(
        _intent(action=Action.SELL), _agent_state(), Decimal("0"), [], _TS
    )
    assert decision.allowed is True


# ── check_letf_auto_liquidations ─────────────────────────────────────────────


def test_auto_liquidations_returns_overdue_letfs() -> None:
    gate, _, lots = _gate()
    from core.types import Fill
    old_ts = datetime(2026, 4, 14, 10, 0, tzinfo=UTC)  # 10 days ago
    buy_fill = Fill(
        id=new_id(), order_id=new_id(), agent_id=AgentId.HAIKU, symbol="UPRO",
        side=OrderSide.BUY, qty=Decimal("5"), price=Decimal("80"), timestamp=old_ts,
    )
    lots.open_lot(buy_fill)
    positions = [_position(symbol="UPRO", qty="5", price="80")]
    to_liq = gate.check_letf_auto_liquidations(AgentId.HAIKU, positions, _TS)
    assert "UPRO" in to_liq


def test_auto_liquidations_fresh_letf_not_returned() -> None:
    gate, _, lots = _gate()
    from core.types import Fill
    recent_ts = datetime(2026, 4, 22, 10, 0, tzinfo=UTC)  # 2 days ago
    buy_fill = Fill(
        id=new_id(), order_id=new_id(), agent_id=AgentId.HAIKU, symbol="TQQQ",
        side=OrderSide.BUY, qty=Decimal("10"), price=Decimal("60"), timestamp=recent_ts,
    )
    lots.open_lot(buy_fill)
    positions = [_position(symbol="TQQQ", qty="10", price="60")]
    to_liq = gate.check_letf_auto_liquidations(AgentId.HAIKU, positions, _TS)
    assert "TQQQ" not in to_liq


def test_auto_liquidations_ignores_non_letf() -> None:
    gate, _, lots = _gate()
    from core.types import Fill
    old_ts = datetime(2026, 4, 14, 10, 0, tzinfo=UTC)
    buy_fill = Fill(
        id=new_id(), order_id=new_id(), agent_id=AgentId.HAIKU, symbol="AAPL",
        side=OrderSide.BUY, qty=Decimal("10"), price=Decimal("200"), timestamp=old_ts,
    )
    lots.open_lot(buy_fill)
    positions = [_position(symbol="AAPL", qty="10", price="200")]
    to_liq = gate.check_letf_auto_liquidations(AgentId.HAIKU, positions, _TS)
    assert "AAPL" not in to_liq
