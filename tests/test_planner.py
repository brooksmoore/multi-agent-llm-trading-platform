"""Tests for execution/planner.py — ExecutionPlanner sizing math.

Covers:
- vol_targeted_position_value() math (floor, cap, normal)
- Long / short / rebalance / close intents
- Options (MLEG) order construction
- Sub-$1 notional rejection
- All 5 drawdown buckets (size cut and FORCED_CASH→None)
- All 5 VIX buckets (scalar applied)
- MASTER_CAPABILITY runtime change reflected in next plan()
- Drawdown ladder boundary: YELLOW cuts exactly 25% vs NORMAL
- IntentSizedEvent emitted to EventBus with correct fields
- Integration: intent → planner → OMS.submit_order → FakeBroker fill → LotLedger updated
- LETF is_letf flag set on TQQQ
- Missing mark price returns None
- CLOSE uses actual lot qty, not target_weight math
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from core.clock import WallClock
from core.events import EventBus, IntentSizedEvent
from core.types import (
    Action,
    AgentId,
    AgentState,
    DrawdownBucket,
    Fill,
    Intent,
    MarketSnapshot,
    OptionLeg,
    OrderClass,
    OrderSide,
    Sleeve,
    VixBucket,
    new_id,
)
from execution.fake_broker import FakeBroker
from execution.lots import LotLedger
from execution.oms import OMS
from execution.oms_store import OMSStore
from execution.planner import ExecutionPlanner
from execution.sizing import vol_targeted_position_value

# ── Helpers ───────────────────────────────────────────────────────────────────

_TS = datetime(2026, 4, 26, 10, 0, tzinfo=UTC)
_EQUITY = Decimal("1000")
_MARK = Decimal("100")  # $100/share — clean math


def _intent(
    action: Action = Action.BUY,
    symbol: str = "SPY",
    target_weight: Decimal = Decimal("0.10"),
    agent_id: AgentId = AgentId.SONNET,
    legs: tuple[OptionLeg, ...] = (),
    sleeve: Sleeve = Sleeve.EQUITY,
) -> Intent:
    return Intent(
        id=new_id(),
        agent_id=agent_id,
        symbol=symbol,
        action=action,
        target_weight=target_weight,
        sleeve=sleeve,
        signal="test_signal",
        conviction=7,
        rationale="test rationale",
        timestamp=_TS,
        legs=legs,
    )


def _agent_state(
    drawdown_bucket: DrawdownBucket = DrawdownBucket.NORMAL,
    equity: Decimal = _EQUITY,
    consecutive_losses: int = 0,
    is_benched: bool = False,
) -> AgentState:
    return AgentState(
        agent_id=AgentId.SONNET,
        sleeve_equity=equity,
        sleeve_peak_equity=equity,
        drawdown_bucket=drawdown_bucket,
        drawdown_bucket_entry_date=None,
        consecutive_losses=consecutive_losses,
        is_benched=is_benched,
        bench_until=None,
        day_trade_count=0,
        orders_today=0,
        last_memo_id=None,
    )


def _snapshot(
    price: Decimal = _MARK,
    symbol: str = "SPY",
    vol: Decimal = Decimal("0.12"),
    vix_bucket: VixBucket = VixBucket.SWEET_SPOT,
) -> MarketSnapshot:
    return MarketSnapshot(
        current_prices={symbol: price},
        realized_vol_30d={symbol: vol},
        vix_bucket=vix_bucket,
        timestamp=_TS,
    )


def _make_planner(
    bus: EventBus | None = None,
    broker: FakeBroker | None = None,
    tmp_path: str | None = None,
) -> tuple[ExecutionPlanner, OMS, LotLedger, EventBus, FakeBroker]:
    bus = bus or EventBus()
    broker = broker or FakeBroker()
    broker.set_price("SPY", _MARK)
    ledger = LotLedger()
    db_path = tmp_path or str(Path(tempfile.mkdtemp()) / "oms.db")
    store = OMSStore(db_path)
    oms = OMS(broker=broker, store=store, bus=bus, clock=WallClock())
    planner = ExecutionPlanner(oms=oms, lot_ledger=ledger, bus=bus)
    return planner, oms, ledger, bus, broker


# ── vol_targeted_position_value unit tests ────────────────────────────────────


class TestVolTargetedPositionValue:
    def test_normal_case(self) -> None:
        # vol_target=0.12, realized=0.12 → mult=1.0
        result = vol_targeted_position_value(
            target_weight=Decimal("0.10"),
            agent_equity=Decimal("1000"),
            realized_vol_annual=Decimal("0.12"),
            effective_vol_target=Decimal("0.12"),
        )
        assert result == Decimal("100")

    def test_cap_at_1_75(self) -> None:
        # vol_target=0.14, realized=0.04 → raw mult=3.5 → capped at 1.75
        result = vol_targeted_position_value(
            target_weight=Decimal("0.10"),
            agent_equity=Decimal("1000"),
            realized_vol_annual=Decimal("0.04"),
            effective_vol_target=Decimal("0.14"),
        )
        assert result == Decimal("0.10") * Decimal("1000") * Decimal("1.75")

    def test_floor_at_8_pct(self) -> None:
        # realized_vol=0.01 → floor to 0.08; vol_target=0.12 → mult=1.5
        result = vol_targeted_position_value(
            target_weight=Decimal("0.10"),
            agent_equity=Decimal("1000"),
            realized_vol_annual=Decimal("0.01"),
            effective_vol_target=Decimal("0.12"),
        )
        expected = Decimal("0.10") * Decimal("1000") * (Decimal("0.12") / Decimal("0.08"))
        assert result == expected

    def test_zero_target_weight(self) -> None:
        result = vol_targeted_position_value(
            target_weight=Decimal("0"),
            agent_equity=Decimal("1000"),
            realized_vol_annual=Decimal("0.12"),
            effective_vol_target=Decimal("0.12"),
        )
        assert result == Decimal("0")


# ── ExecutionPlanner unit tests ───────────────────────────────────────────────


class TestPlannerBasic:
    def test_long_intent_returns_buy_order(self) -> None:
        planner, *_ = _make_planner()
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(_intent(), _agent_state(), _snapshot())
        assert order is not None
        assert order.side == OrderSide.BUY
        assert order.order_class == OrderClass.SIMPLE
        assert order.qty > Decimal("0")

    def test_sell_intent_returns_sell_order(self) -> None:
        planner, _oms, ledger, *_ = _make_planner()
        # Seed an open lot — the planner refuses SELL when the agent has none.
        ledger.open_lot(Fill(
            id=new_id(),
            order_id=new_id(),
            agent_id=AgentId.SONNET,
            symbol="SPY",
            side=OrderSide.BUY,
            qty=Decimal("10"),
            price=Decimal("100"),
            timestamp=_TS,
        ))
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(
                _intent(action=Action.SELL), _agent_state(), _snapshot()
            )
        assert order is not None
        assert order.side == OrderSide.SELL

    def test_sell_intent_rejected_when_no_open_lots(self) -> None:
        # Fail-safe against hallucinated sells.
        planner, *_ = _make_planner()
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(
                _intent(action=Action.SELL), _agent_state(), _snapshot()
            )
        assert order == "unsized:no_position"

    def test_rebalance_to_returns_buy_order(self) -> None:
        planner, *_ = _make_planner()
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(
                _intent(action=Action.REBALANCE_TO), _agent_state(), _snapshot()
            )
        assert order is not None
        assert order.side == OrderSide.BUY

    def test_fractional_qty_for_equity(self) -> None:
        planner, *_ = _make_planner()
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            # target_weight=0.10, equity=1000, realized_vol=0.12, mark=100.
            # Sonnet target_vol=0.175 → vol-target multiplier 1.458 →
            # position_value ≈ $145.83 → qty ≈ 1.458 (fractional).
            order = planner.plan(_intent(), _agent_state(), _snapshot())
        assert not isinstance(order, str)
        assert isinstance(order.qty, Decimal)
        assert order.qty > Decimal("0")
        assert order.qty != order.qty.to_integral_value()  # confirm fractional

    def test_sub_dollar_notional_returns_none(self) -> None:
        planner, *_ = _make_planner()
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(
                _intent(target_weight=Decimal("0.000001")),
                _agent_state(equity=Decimal("10")),
                _snapshot(price=Decimal("1000")),
            )
        assert order == "unsized:sub_min"

    def test_missing_mark_price_returns_none(self) -> None:
        planner, *_ = _make_planner()
        snap = MarketSnapshot(
            current_prices={},  # empty — SPY not present
            realized_vol_30d={"SPY": Decimal("0.12")},
            vix_bucket=VixBucket.SWEET_SPOT,
            timestamp=_TS,
        )
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(_intent(), _agent_state(), snap)
        assert order == "unsized:no_mark"

    def test_letf_sets_is_letf_flag(self) -> None:
        planner, *_, broker = _make_planner()
        broker.set_price("TQQQ", _MARK)
        snap = _snapshot(symbol="TQQQ")
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(_intent(symbol="TQQQ"), _agent_state(), snap)
        assert order is not None
        assert order.is_letf is True

    def test_non_letf_does_not_set_is_letf(self) -> None:
        planner, *_ = _make_planner()
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(_intent(symbol="SPY"), _agent_state(), _snapshot())
        assert order is not None
        assert order.is_letf is False


# ── Drawdown bucket tests ─────────────────────────────────────────────────────


class TestDrawdownBuckets:
    def _plan_with_bucket(
        self, bucket: DrawdownBucket, planner: ExecutionPlanner
    ) -> Decimal | None:
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(
                _intent(target_weight=Decimal("0.10")),
                _agent_state(drawdown_bucket=bucket),
                _snapshot(),
            )
        return order.qty if not isinstance(order, str) else None

    def test_normal_bucket_full_size(self) -> None:
        planner, *_ = _make_planner()
        qty = self._plan_with_bucket(DrawdownBucket.NORMAL, planner)
        assert qty is not None

    def test_yellow_bucket_cuts_size(self) -> None:
        planner, *_ = _make_planner()
        normal_qty = self._plan_with_bucket(DrawdownBucket.NORMAL, planner)
        yellow_qty = self._plan_with_bucket(DrawdownBucket.YELLOW, planner)
        assert normal_qty is not None and yellow_qty is not None
        # YELLOW scalar = 0.75; size should be ≤ normal (may bind on vol-target)
        assert yellow_qty <= normal_qty

    def test_orange_bucket_cuts_further(self) -> None:
        planner, *_ = _make_planner()
        yellow_qty = self._plan_with_bucket(DrawdownBucket.YELLOW, planner)
        orange_qty = self._plan_with_bucket(DrawdownBucket.ORANGE, planner)
        assert yellow_qty is not None and orange_qty is not None
        assert orange_qty <= yellow_qty

    def test_red_bucket(self) -> None:
        planner, *_ = _make_planner()
        orange_qty = self._plan_with_bucket(DrawdownBucket.ORANGE, planner)
        red_qty = self._plan_with_bucket(DrawdownBucket.RED, planner)
        assert orange_qty is not None and red_qty is not None
        assert red_qty <= orange_qty

    def test_forced_cash_returns_none(self) -> None:
        planner, *_ = _make_planner()
        qty = self._plan_with_bucket(DrawdownBucket.FORCED_CASH, planner)
        assert qty is None

    def test_yellow_exactly_75_pct_of_normal_when_gross_binds(self) -> None:
        """When max_gross binds, YELLOW bucket is exactly 75% of NORMAL bucket qty.

        Setup: target_weight=1.0, realized_vol=0.08 floor → sizing_mult=0.12/0.08=1.5.
        SONNET base_max_gross=1.25: vol_targeted(1.0×1000×1.5=1500) > gross_cap(1250).
        → max_gross binds.  YELLOW scalar=0.75 → 750 notional → 7.5 qty.
        """
        planner, *_ = _make_planner()
        snap = MarketSnapshot(
            current_prices={"SPY": _MARK},
            realized_vol_30d={"SPY": Decimal("0.08")},  # at floor: sizing_mult=1.5
            vix_bucket=VixBucket.SWEET_SPOT,
            timestamp=_TS,
        )

        def _plan(bucket: DrawdownBucket) -> Decimal:
            with patch("execution.planner.runtime_store") as mock_rs:
                mock_rs.master_capability = Decimal("1.0")
                order = planner.plan(
                    _intent(target_weight=Decimal("1.0")),  # large weight → gross binds
                    _agent_state(drawdown_bucket=bucket),
                    snap,
                )
            assert order is not None, f"Expected order for {bucket}"
            return order.qty

        normal_qty = _plan(DrawdownBucket.NORMAL)
        yellow_qty = _plan(DrawdownBucket.YELLOW)
        assert yellow_qty == pytest.approx(float(normal_qty) * 0.75, rel=1e-6)


# ── VIX bucket tests ──────────────────────────────────────────────────────────


class TestVixBuckets:
    def _plan_with_vix(
        self,
        vix_bucket: VixBucket,
        planner: ExecutionPlanner,
    ) -> Decimal | None:
        # target_weight=1.0 so max_gross always binds (vol_targeted > gross_cap)
        snap = MarketSnapshot(
            current_prices={"SPY": _MARK},
            realized_vol_30d={"SPY": Decimal("0.08")},  # at vol floor: sizing_mult=1.5
            vix_bucket=vix_bucket,
            timestamp=_TS,
        )
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(
                _intent(target_weight=Decimal("1.0")),  # large weight → gross binds
                _agent_state(),
                snap,
            )
        return order.qty if not isinstance(order, str) else None

    def test_all_five_vix_buckets_scale_monotonically(self) -> None:
        """Ordered: SWEET_SPOT > ELEVATED > VERY_LOW > STRESS > CRISIS (by scalar)."""
        planner, *_ = _make_planner()
        buckets_ordered_by_scalar = [
            VixBucket.SWEET_SPOT,   # 1.0
            VixBucket.ELEVATED,     # 0.8
            VixBucket.VERY_LOW,     # 0.6
            VixBucket.STRESS,       # 0.5
            VixBucket.CRISIS,       # 0.25
        ]
        qtys = [self._plan_with_vix(b, planner) for b in buckets_ordered_by_scalar]
        assert all(q is not None for q in qtys)
        # Each should be <= the previous (weakly monotone — rounding may tie)
        for i in range(len(qtys) - 1):
            assert qtys[i] >= qtys[i + 1]  # type: ignore[operator]

    def test_crisis_returns_very_small_size(self) -> None:
        planner, *_ = _make_planner()
        sweet = self._plan_with_vix(VixBucket.SWEET_SPOT, planner)
        crisis = self._plan_with_vix(VixBucket.CRISIS, planner)
        assert sweet is not None and crisis is not None
        assert crisis < sweet


# ── MASTER_CAPABILITY runtime tests ──────────────────────────────────────────


class TestMasterCapabilityRuntime:
    def test_mc_change_reflected_in_next_plan(self) -> None:
        """MC slider change at runtime affects next plan() call without restart."""
        planner, *_ = _make_planner()
        snap = MarketSnapshot(
            current_prices={"SPY": _MARK},
            realized_vol_30d={"SPY": Decimal("0.20")},  # vol-target binds, no saturation
            vix_bucket=VixBucket.SWEET_SPOT,
            timestamp=_TS,
        )
        intent = _intent(target_weight=Decimal("0.10"))
        state = _agent_state()

        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order_1 = planner.plan(intent, state, snap)

        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("0.5")  # halve MC
            order_2 = planner.plan(intent, state, snap)

        assert not isinstance(order_1, str) and not isinstance(order_2, str)
        # MC=0.5 → half the effective_max_gross → half the qty
        assert float(order_2.qty) == pytest.approx(float(order_1.qty) * 0.5, rel=1e-6)

    def test_mc_zero_returns_none(self) -> None:
        planner, *_ = _make_planner()
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("0")
            order = planner.plan(_intent(), _agent_state(), _snapshot())
        assert isinstance(order, str)  # FORCED_CASH or sub-min


# ── Options (MLEG) tests ──────────────────────────────────────────────────────


class TestOptionsPlanning:
    def _option_intent(self) -> Intent:
        leg_buy = OptionLeg(symbol="SPY260620C00500000", side=OrderSide.BUY, ratio_qty=1)
        leg_sell = OptionLeg(symbol="SPY260620C00510000", side=OrderSide.SELL, ratio_qty=1)
        return Intent(
            id=new_id(),
            agent_id=AgentId.SONNET,
            symbol="SPY",
            action=Action.BUY,
            target_weight=Decimal("0.05"),
            sleeve=Sleeve.OPTIONS,
            signal="spread_signal",
            conviction=6,
            rationale="debit spread",
            timestamp=_TS,
            legs=(leg_buy, leg_sell),
        )

    def test_options_intent_creates_mleg_order(self) -> None:
        planner, *_ = _make_planner()
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(
                self._option_intent(),
                _agent_state(equity=Decimal("10000")),
                _snapshot(price=Decimal("5")),  # $5 premium
            )
        assert order is not None
        assert order.order_class == OrderClass.MLEG
        assert len(order.legs) == 2

    def test_options_qty_is_whole_contracts(self) -> None:
        planner, *_ = _make_planner()
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(
                self._option_intent(),
                _agent_state(equity=Decimal("10000")),
                _snapshot(price=Decimal("5")),  # $5 premium → $500/contract
            )
        assert order is not None
        # qty must be an integer value (no fractional contracts)
        assert order.qty == order.qty.to_integral_value()

    def test_tiny_options_premium_returns_none(self) -> None:
        """If position_value < 1 contract premium × 100, return None."""
        planner, *_ = _make_planner()
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(
                self._option_intent(),
                _agent_state(equity=Decimal("10")),   # tiny sleeve
                _snapshot(price=Decimal("500")),       # expensive option
            )
        assert isinstance(order, str)  # tiny premium


# ── IntentSizedEvent emission ─────────────────────────────────────────────────


class TestIntentSizedEvent:
    def test_event_emitted_on_successful_plan(self) -> None:
        bus = EventBus()
        planner, *_ = _make_planner(bus=bus)

        events: list[IntentSizedEvent] = []
        bus.subscribe("intent.sized", lambda e: events.append(e))  # type: ignore[arg-type]

        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(_intent(), _agent_state(), _snapshot())

        assert order is not None
        assert len(events) == 1
        ev = events[0]
        assert ev.symbol == "SPY"
        assert ev.agent_id == AgentId.SONNET
        assert ev.qty > Decimal("0")
        assert ev.position_value_usd > Decimal("0")

    def test_no_event_on_sub_dollar_rejection(self) -> None:
        bus = EventBus()
        planner, *_ = _make_planner(bus=bus)

        events: list[IntentSizedEvent] = []
        bus.subscribe("intent.sized", lambda e: events.append(e))  # type: ignore[arg-type]

        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(
                _intent(target_weight=Decimal("0.000001")),
                _agent_state(equity=Decimal("1")),
                _snapshot(),
            )

        assert isinstance(order, str)  # sub-$1
        assert len(events) == 0

    def test_event_binding_constraint_field(self) -> None:
        bus = EventBus()
        planner, *_ = _make_planner(bus=bus)

        events: list[IntentSizedEvent] = []
        bus.subscribe("intent.sized", lambda e: events.append(e))  # type: ignore[arg-type]

        # target_weight=1.0 at vol floor → vol_targeted=1500 > gross_cap=1250 → max_gross binds
        snap = MarketSnapshot(
            current_prices={"SPY": _MARK},
            realized_vol_30d={"SPY": Decimal("0.08")},
            vix_bucket=VixBucket.SWEET_SPOT,
            timestamp=_TS,
        )
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            planner.plan(_intent(target_weight=Decimal("1.0")), _agent_state(), snap)

        assert events[0].binding_constraint == "max_gross"


# ── CLOSE intent tests ────────────────────────────────────────────────────────


class TestCloseIntent:
    def _seed_lot(self, ledger: LotLedger, qty: Decimal = Decimal("3")) -> None:
        """Open a lot in the ledger so CLOSE has something to close."""
        fill = Fill(
            id=new_id(),
            order_id=new_id(),
            agent_id=AgentId.SONNET,
            symbol="SPY",
            side=OrderSide.BUY,
            qty=qty,
            price=_MARK,
            timestamp=_TS,
        )
        ledger.open_lot(fill)

    def test_close_uses_open_lot_qty(self) -> None:
        planner, oms, ledger, bus, broker = _make_planner()
        self._seed_lot(ledger, qty=Decimal("3.5"))

        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(
                _intent(action=Action.CLOSE),
                _agent_state(),
                _snapshot(),
            )

        assert order is not None
        assert order.qty == Decimal("3.5")
        assert order.side == OrderSide.SELL

    def test_close_no_open_lots_returns_none(self) -> None:
        planner, *_ = _make_planner()
        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(
                _intent(action=Action.CLOSE),
                _agent_state(),
                _snapshot(),
            )
        assert order == "unsized:no_position"


# ── Integration: full chain ───────────────────────────────────────────────────


class TestFullChainIntegration:
    def test_plan_submit_fill_ledger(self, tmp_path: pytest.TempPathFactory) -> None:
        """intent → planner.plan() → OMS.submit_order → FakeBroker fill → ledger updated."""
        broker = FakeBroker(starting_cash=Decimal("30000"))
        broker.set_price("SPY", _MARK)
        bus = EventBus()
        ledger = LotLedger()
        clock = WallClock()
        store = OMSStore(str(tmp_path / "oms.db"))
        oms = OMS(broker=broker, store=store, bus=bus, clock=clock)
        planner = ExecutionPlanner(oms=oms, lot_ledger=ledger, bus=bus)

        # Wire FillReceived → LotLedger (mimicking app.py wiring)
        from core.events import FillReceivedEvent

        def _on_fill(event: FillReceivedEvent) -> None:  # type: ignore[misc]
            if event.fill.side == OrderSide.BUY:
                ledger.open_lot(event.fill)

        bus.subscribe("fill.received", _on_fill)  # type: ignore[arg-type]

        intent = _intent(target_weight=Decimal("0.10"))
        state = _agent_state()
        snap = _snapshot()

        with patch("execution.planner.runtime_store") as mock_rs:
            mock_rs.master_capability = Decimal("1.0")
            order = planner.plan(intent, state, snap)

        assert order is not None
        result = oms.submit_order(order)
        assert result.accepted

        # FakeBroker INSTANT mode fills synchronously; lot should be opened
        open_qty = ledger.total_open_qty(AgentId.SONNET, "SPY")
        assert open_qty > Decimal("0")
        assert open_qty == order.qty
