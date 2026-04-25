"""End-to-end OMS lifecycle tests against the FakeBroker.

Covers (no recovery — that's test_oms_recovery.py):
- Happy-path INSTANT submit → ACCEPTED → FILLED, with all log events written
- MANUAL submit → ACCEPTED, then external force_full_fill → FILLED
- Partial fill → another partial fill → final full fill
- Broker rejection (REJECT mode) routes through OMS to OrderState.REJECTED
- Cancel an open order → OrderState.CANCELLED
- Cancel before broker_order_id assigned (synthetic local cancel)
- Multiple concurrent orders for different agents
- EventBus receives OrderPlacedEvent + OrderStateChangedEvent + FillReceivedEvent
- BrokerUnavailable propagates and leaves order in SUBMITTED-but-not-ACCEPTED state
- submit_order rejects non-PENDING orders
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from core.clock import BacktestClock
from core.events import (
    Event,
    EventBus,
    FillReceivedEvent,
    OrderStateChangedEvent,
)
from core.types import AgentId, OrderSide, OrderState
from execution.broker import BrokerUnavailable
from execution.fake_broker import FakeBroker, FillMode, make_market_order
from execution.oms import OMS
from execution.oms_store import EventKind, OMSStore

# ─── Test fixture helpers ─────────────────────────────────────────────────────


def _wire_oms(
    tmp_path: Path,
    fill_mode: FillMode = FillMode.INSTANT,
    starting_cash: Decimal = Decimal("30000"),
) -> tuple[OMS, FakeBroker, OMSStore, EventBus, BacktestClock, list[Event]]:
    """Build a fully-wired OMS + FakeBroker + collected event log for assertions."""
    clock = BacktestClock(datetime(2026, 4, 24, 14, 0, tzinfo=UTC))
    broker = FakeBroker(clock=clock, fill_mode=fill_mode, starting_cash=starting_cash)
    broker.set_price("SPY", Decimal("450"))
    broker.set_price("QQQ", Decimal("400"))
    broker.set_price("AAPL", Decimal("200"))
    store = OMSStore(tmp_path / "oms.db")
    bus = EventBus()
    captured: list[Event] = []
    bus.subscribe_all(captured.append)
    oms = OMS(broker=broker, store=store, bus=bus, clock=clock)
    return oms, broker, store, bus, clock, captured


# ─── Happy paths ──────────────────────────────────────────────────────────────


class TestHappyPath:

    def test_instant_buy_fills_completely(self, tmp_path: Path) -> None:
        oms, broker, store, _bus, _clock, captured = _wire_oms(tmp_path)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
        )
        result = oms.submit_order(order)
        assert result.accepted is True
        assert result.broker_order_id is not None
        final = oms.get_order(order.id)
        assert final is not None
        assert final.state == OrderState.FILLED
        assert final.filled_qty == Decimal("10")
        assert final.filled_avg_price == Decimal("450")

    def test_log_records_full_lifecycle(self, tmp_path: Path) -> None:
        oms, _broker, store, _bus, _clock, _captured = _wire_oms(tmp_path)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
        )
        oms.submit_order(order)
        kinds = [e.kind for e in store.iter_for_order(order.id)]
        assert kinds == [
            EventKind.ORDER_SUBMIT_INTENT,
            EventKind.ORDER_ACCEPTED,
            EventKind.FILL_RECEIVED,
        ]

    def test_bus_receives_all_events(self, tmp_path: Path) -> None:
        oms, _broker, _store, _bus, _clock, captured = _wire_oms(tmp_path)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
        )
        oms.submit_order(order)
        types_seen = [type(e).__name__ for e in captured]
        # Must include OrderPlaced, multiple OrderStateChanged, FillReceived
        assert "OrderPlacedEvent" in types_seen
        assert "FillReceivedEvent" in types_seen
        # OrderStateChanged fires on every transition: SUBMIT → ACCEPT → FULL_FILL
        state_changes = [e for e in captured if isinstance(e, OrderStateChangedEvent)]
        assert len(state_changes) == 3
        assert state_changes[0].new_state == OrderState.SUBMITTED
        assert state_changes[1].new_state == OrderState.ACCEPTED
        assert state_changes[2].new_state == OrderState.FILLED

    def test_get_fills_returns_fill(self, tmp_path: Path) -> None:
        oms, _broker, _store, _bus, _clock, _captured = _wire_oms(tmp_path)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("3"), agent_id=AgentId.SONNET,
        )
        oms.submit_order(order)
        fills = oms.get_fills(order.id)
        assert len(fills) == 1
        assert fills[0].qty == Decimal("3")
        assert fills[0].agent_id == AgentId.SONNET


# ─── Manual mode (deferred fills) ─────────────────────────────────────────────


class TestManualFills:

    def test_manual_submit_then_external_full_fill(self, tmp_path: Path) -> None:
        oms, broker, _store, _bus, _clock, _captured = _wire_oms(tmp_path, FillMode.MANUAL)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("2"), agent_id=AgentId.HAIKU,
        )
        oms.submit_order(order)
        assert oms.get_order(order.id).state == OrderState.ACCEPTED
        broker.force_full_fill(order.id)
        assert oms.get_order(order.id).state == OrderState.FILLED

    def test_partial_then_partial_then_full(self, tmp_path: Path) -> None:
        oms, broker, store, _bus, _clock, _captured = _wire_oms(tmp_path, FillMode.MANUAL)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.OPUS,
        )
        oms.submit_order(order)
        broker.force_partial_fill(order.id, qty=Decimal("3"), price=Decimal("450"))
        o = oms.get_order(order.id)
        assert o.state == OrderState.PARTIAL
        assert o.filled_qty == Decimal("3")

        broker.force_partial_fill(order.id, qty=Decimal("4"), price=Decimal("451"))
        o = oms.get_order(order.id)
        assert o.state == OrderState.PARTIAL
        assert o.filled_qty == Decimal("7")
        # Average price weighted: (3*450 + 4*451) / 7
        expected_avg = (
            Decimal("3") * Decimal("450") + Decimal("4") * Decimal("451")
        ) / Decimal("7")
        assert o.filled_avg_price == expected_avg

        broker.force_full_fill(order.id, price=Decimal("452"))
        o = oms.get_order(order.id)
        assert o.state == OrderState.FILLED
        assert o.filled_qty == Decimal("10")

        # Three fills in the log
        fills = oms.get_fills(order.id)
        assert len(fills) == 3
        assert sum((f.qty for f in fills), Decimal("0")) == Decimal("10")


# ─── Rejection ────────────────────────────────────────────────────────────────


class TestRejection:

    def test_broker_reject_lands_in_rejected_state(self, tmp_path: Path) -> None:
        oms, _broker, store, _bus, _clock, _captured = _wire_oms(tmp_path, FillMode.REJECT)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        result = oms.submit_order(order)
        assert result.accepted is False
        assert result.rejection_reason is not None
        o = oms.get_order(order.id)
        assert o.state == OrderState.REJECTED
        assert o.rejection_reason

    def test_reject_log_contains_intent_then_rejected(self, tmp_path: Path) -> None:
        oms, _broker, store, _bus, _clock, _captured = _wire_oms(tmp_path, FillMode.REJECT)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        oms.submit_order(order)
        kinds = [e.kind for e in store.iter_for_order(order.id)]
        assert EventKind.ORDER_SUBMIT_INTENT in kinds
        assert EventKind.ORDER_REJECTED in kinds


# ─── Cancellation ─────────────────────────────────────────────────────────────


class TestCancel:

    def test_cancel_open_order(self, tmp_path: Path) -> None:
        oms, _broker, _store, _bus, _clock, _captured = _wire_oms(tmp_path, FillMode.MANUAL)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        oms.submit_order(order)
        assert oms.get_order(order.id).state == OrderState.ACCEPTED
        oms.cancel_order(order.id)
        assert oms.get_order(order.id).state == OrderState.CANCELLED

    def test_cancel_unknown_raises(self, tmp_path: Path) -> None:
        oms, _broker, _store, _bus, _clock, _captured = _wire_oms(tmp_path)
        from core.types import new_id  # noqa: PLC0415
        with pytest.raises(KeyError):
            oms.cancel_order(new_id())

    def test_cancel_filled_is_noop(self, tmp_path: Path) -> None:
        oms, _broker, _store, _bus, _clock, _captured = _wire_oms(tmp_path, FillMode.INSTANT)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        oms.submit_order(order)
        # No raise; just no-op
        oms.cancel_order(order.id)
        assert oms.get_order(order.id).state == OrderState.FILLED


# ─── Broker unavailable ───────────────────────────────────────────────────────


class TestBrokerUnavailable:

    def test_broker_unavailable_propagates(self, tmp_path: Path) -> None:
        oms, broker, store, _bus, _clock, _captured = _wire_oms(tmp_path)
        broker.inject_submit_failure(BrokerUnavailable("simulated outage"))
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        with pytest.raises(BrokerUnavailable):
            oms.submit_order(order)
        # Log contains SUBMIT_INTENT but no ACCEPTED/REJECTED — recover() will resolve
        kinds = [e.kind for e in store.iter_for_order(order.id)]
        assert kinds == [EventKind.ORDER_SUBMIT_INTENT]
        # Local order is in SUBMITTED state, awaiting reconciliation
        assert oms.get_order(order.id).state == OrderState.SUBMITTED


# ─── Multiple agents / orders ────────────────────────────────────────────────


class TestMultipleOrders:

    def test_multiple_agents_independent(self, tmp_path: Path) -> None:
        oms, _broker, _store, _bus, _clock, _captured = _wire_oms(tmp_path)
        o1 = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        o2 = make_market_order(
            symbol="QQQ", side=OrderSide.BUY, qty=Decimal("2"), agent_id=AgentId.SONNET,
        )
        o3 = make_market_order(
            symbol="AAPL", side=OrderSide.BUY, qty=Decimal("3"), agent_id=AgentId.OPUS,
        )
        for o in (o1, o2, o3):
            oms.submit_order(o)
        assert all(oms.get_order(o.id).state == OrderState.FILLED for o in (o1, o2, o3))
        assert oms.get_fills(o1.id)[0].agent_id == AgentId.HAIKU
        assert oms.get_fills(o2.id)[0].agent_id == AgentId.SONNET
        assert oms.get_fills(o3.id)[0].agent_id == AgentId.OPUS

    def test_list_open_orders(self, tmp_path: Path) -> None:
        oms, broker, _store, _bus, _clock, _captured = _wire_oms(tmp_path, FillMode.MANUAL)
        o1 = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        o2 = make_market_order(
            symbol="QQQ", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        oms.submit_order(o1)
        oms.submit_order(o2)
        assert len(oms.list_open_orders()) == 2
        broker.force_full_fill(o1.id)
        assert len(oms.list_open_orders()) == 1


# ─── Validation ───────────────────────────────────────────────────────────────


class TestValidation:

    def test_submit_non_pending_raises(self, tmp_path: Path) -> None:
        oms, _broker, _store, _bus, _clock, _captured = _wire_oms(tmp_path)
        from dataclasses import replace  # noqa: PLC0415
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        bogus = replace(order, state=OrderState.FILLED)
        with pytest.raises(ValueError, match="PENDING"):
            oms.submit_order(bogus)


# ─── Fill payload integrity ───────────────────────────────────────────────────


class TestFillIntegrity:

    def test_broker_initiated_cancel_via_callback(self, tmp_path: Path) -> None:
        """Broker emits CANCELED (e.g. exchange cancelled) without us asking; OMS reacts."""
        oms, broker, _store, _bus, _clock, _captured = _wire_oms(tmp_path, FillMode.MANUAL)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        result = oms.submit_order(order)
        assert result.broker_order_id is not None
        # Simulate exchange-side cancel via direct broker.cancel_order
        broker.cancel_order(result.broker_order_id)
        assert oms.get_order(order.id).state == OrderState.CANCELLED

    def test_callback_for_unknown_order_is_safely_ignored(self, tmp_path: Path) -> None:
        """A stray broker event for an order_id we don't know about logs a warning
        and returns; it does NOT crash the OMS."""
        oms, _broker, _store, _bus, _clock, _captured = _wire_oms(tmp_path)
        from core.types import new_id  # noqa: PLC0415
        from execution.broker import BrokerOrderEvent, BrokerOrderState  # noqa: PLC0415
        bogus = BrokerOrderEvent(
            broker_order_id="ghost-id",
            client_order_id=new_id(),
            new_state=BrokerOrderState.FILLED,
        )
        oms._on_broker_event(bogus)  # noqa: SLF001 — direct callback test

    def test_cancel_before_broker_accept_local_only(self, tmp_path: Path) -> None:
        """If we cancel before the broker assigns an ID, mark CANCELLED locally."""
        oms, broker, _store, _bus, _clock, _captured = _wire_oms(tmp_path, FillMode.MANUAL)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        # Hand-craft a state where the order is in our table but never reached the broker
        from core.state_machine import build_order_fsm  # noqa: PLC0415
        oms._orders[order.id] = order  # noqa: SLF001
        oms._fsms[order.id] = build_order_fsm(OrderState.SUBMITTED)  # noqa: SLF001
        oms._fills_by_order[order.id] = []  # noqa: SLF001
        # Now order has no broker_order_id; cancel routes to "local cancel"
        oms.cancel_order(order.id)
        assert oms.get_order(order.id).state == OrderState.CANCELLED

    def test_fill_carries_correct_agent_id_even_if_broker_stamps_default(
        self, tmp_path: Path,
    ) -> None:
        """The OMS guarantees Fill.agent_id matches the originating Order.agent_id,
        even if the broker layer stamped a placeholder."""
        oms, _broker, _store, _bus, _clock, _captured = _wire_oms(tmp_path)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.OPUS,
        )
        oms.submit_order(order)
        fill = oms.get_fills(order.id)[0]
        assert fill.agent_id == AgentId.OPUS
        # Bus event carries the same agent_id
        bus_fills = [e for e in _captured if isinstance(e, FillReceivedEvent)]
        assert bus_fills[0].fill.agent_id == AgentId.OPUS
