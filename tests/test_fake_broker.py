"""Contract tests for FakeBroker.

Locks down the behaviour the OMS depends on:
- Idempotency on client_order_id
- find_order_by_client_id round-trip
- Callback delivery on every state change
- Configurable fill modes (INSTANT, MANUAL, REJECT, PARTIAL_THEN_HOLD)
- Force-* test knobs (force_full_fill, force_partial_fill, force_reject)
- Failure injection (inject_submit_failure)
- Account / position tracking after fills
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.clock import BacktestClock
from core.types import AgentId, OrderSide
from execution.broker import (
    BrokerOrderEvent,
    BrokerOrderState,
    BrokerRejection,
    BrokerUnavailable,
)
from execution.fake_broker import FakeBroker, FillMode, make_market_order


def _clock() -> BacktestClock:
    return BacktestClock(datetime(2026, 4, 24, 14, 0, tzinfo=UTC))


def _captured_events() -> tuple[list[BrokerOrderEvent], FakeBroker]:
    bc = _clock()
    broker = FakeBroker(clock=bc, fill_mode=FillMode.INSTANT)
    events: list[BrokerOrderEvent] = []
    broker.register_event_callback(events.append)
    return events, broker


# ─── Submit happy paths ───────────────────────────────────────────────────────


class TestSubmitHappyPath:

    def test_instant_fill_emits_accepted_then_filled(self) -> None:
        events, broker = _captured_events()
        broker.set_price("SPY", Decimal("450.00"))
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
        )
        broker_id = broker.submit_order(order)
        assert isinstance(broker_id, str)

        # ACCEPTED + FILLED were emitted (in order)
        states = [e.new_state for e in events]
        assert states == [BrokerOrderState.ACCEPTED, BrokerOrderState.FILLED]
        assert events[1].fill is not None
        assert events[1].fill.qty == Decimal("10")
        assert events[1].fill.price == Decimal("450.00")
        assert events[1].fill.agent_id == AgentId.HAIKU

    def test_manual_mode_does_not_auto_fill(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.MANUAL)
        events: list[BrokerOrderEvent] = []
        broker.register_event_callback(events.append)
        order = make_market_order(
            symbol="QQQ", side=OrderSide.BUY, qty=Decimal("5"), agent_id=AgentId.SONNET,
        )
        broker.submit_order(order)
        # Only ACCEPTED, no FILLED
        assert [e.new_state for e in events] == [BrokerOrderState.ACCEPTED]

    def test_partial_then_hold_emits_partial_only(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.PARTIAL_THEN_HOLD)
        broker.set_price("AAPL", Decimal("200"))
        events: list[BrokerOrderEvent] = []
        broker.register_event_callback(events.append)
        order = make_market_order(
            symbol="AAPL", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.OPUS,
        )
        broker.submit_order(order)
        states = [e.new_state for e in events]
        assert states == [BrokerOrderState.ACCEPTED, BrokerOrderState.PARTIALLY_FILLED]
        assert events[1].fill is not None
        assert events[1].fill.qty == Decimal("5")  # half


# ─── Idempotency ──────────────────────────────────────────────────────────────


class TestIdempotency:

    def test_same_order_id_returns_same_broker_id(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.MANUAL)
        broker.register_event_callback(lambda _: None)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        bid_first = broker.submit_order(order)
        bid_second = broker.submit_order(order)
        assert bid_first == bid_second

    def test_idempotent_submit_does_not_duplicate_events(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.MANUAL)
        events: list[BrokerOrderEvent] = []
        broker.register_event_callback(events.append)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        broker.submit_order(order)
        broker.submit_order(order)
        assert len(events) == 1   # ACCEPTED once

    def test_find_by_client_id_after_submit(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.MANUAL)
        broker.register_event_callback(lambda _: None)
        order = make_market_order(
            symbol="MSFT", side=OrderSide.BUY, qty=Decimal("3"), agent_id=AgentId.SONNET,
        )
        broker.submit_order(order)
        status = broker.find_order_by_client_id(order.id)
        assert status is not None
        assert status.symbol == "MSFT"
        assert status.client_order_id == order.id

    def test_find_by_client_id_unknown_returns_none(self) -> None:
        broker = FakeBroker(clock=_clock())
        from core.types import new_id  # noqa: PLC0415
        assert broker.find_order_by_client_id(new_id()) is None


# ─── Reject mode ──────────────────────────────────────────────────────────────


class TestRejectMode:

    def test_reject_mode_raises_and_emits_rejected(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.REJECT)
        events: list[BrokerOrderEvent] = []
        broker.register_event_callback(events.append)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        with pytest.raises(BrokerRejection):
            broker.submit_order(order)
        # Even though it raised, broker recorded the order so find_order_by_client_id works
        status = broker.find_order_by_client_id(order.id)
        assert status is not None
        assert status.state == BrokerOrderState.REJECTED
        assert events[-1].new_state == BrokerOrderState.REJECTED


# ─── Force-* test knobs ───────────────────────────────────────────────────────


class TestForceFills:

    def test_force_full_fill(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.MANUAL)
        broker.set_price("SPY", Decimal("400"))
        events: list[BrokerOrderEvent] = []
        broker.register_event_callback(events.append)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("5"), agent_id=AgentId.HAIKU,
        )
        broker.submit_order(order)
        broker.force_full_fill(order.id)
        assert events[-1].new_state == BrokerOrderState.FILLED
        assert events[-1].fill is not None
        assert events[-1].fill.qty == Decimal("5")
        assert events[-1].fill.price == Decimal("400")

    def test_force_partial_then_force_full(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.MANUAL)
        broker.set_price("SPY", Decimal("400"))
        events: list[BrokerOrderEvent] = []
        broker.register_event_callback(events.append)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
        )
        broker.submit_order(order)
        broker.force_partial_fill(order.id, qty=Decimal("3"))
        assert events[-1].new_state == BrokerOrderState.PARTIALLY_FILLED
        broker.force_partial_fill(order.id, qty=Decimal("4"))
        assert events[-1].new_state == BrokerOrderState.PARTIALLY_FILLED
        broker.force_full_fill(order.id)
        assert events[-1].new_state == BrokerOrderState.FILLED

    def test_force_partial_fill_oversized_raises(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.MANUAL)
        broker.register_event_callback(lambda _: None)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("5"), agent_id=AgentId.HAIKU,
        )
        broker.submit_order(order)
        with pytest.raises(ValueError, match="exceeds remaining"):
            broker.force_partial_fill(order.id, qty=Decimal("10"))

    def test_force_full_fill_unknown_raises(self) -> None:
        broker = FakeBroker(clock=_clock())
        from core.types import new_id  # noqa: PLC0415
        with pytest.raises(KeyError):
            broker.force_full_fill(new_id())

    def test_force_reject(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.MANUAL)
        events: list[BrokerOrderEvent] = []
        broker.register_event_callback(events.append)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        broker.submit_order(order)
        broker.force_reject(order.id, reason="insufficient buying power")
        assert events[-1].new_state == BrokerOrderState.REJECTED
        assert events[-1].rejection_reason == "insufficient buying power"


# ─── Cancel ───────────────────────────────────────────────────────────────────


class TestCancel:

    def test_cancel_open_order(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.MANUAL)
        events: list[BrokerOrderEvent] = []
        broker.register_event_callback(events.append)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        broker_id = broker.submit_order(order)
        broker.cancel_order(broker_id)
        assert events[-1].new_state == BrokerOrderState.CANCELED

    def test_cancel_filled_is_noop(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.INSTANT)
        broker.set_price("SPY", Decimal("100"))
        events: list[BrokerOrderEvent] = []
        broker.register_event_callback(events.append)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        broker_id = broker.submit_order(order)
        events_before = len(events)
        broker.cancel_order(broker_id)
        assert len(events) == events_before  # no extra events


# ─── Failure injection ────────────────────────────────────────────────────────


class TestFailureInjection:

    def test_inject_submit_failure_raises_then_recovers(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.INSTANT)
        broker.register_event_callback(lambda _: None)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        broker.inject_submit_failure(BrokerUnavailable("simulated"))
        with pytest.raises(BrokerUnavailable):
            broker.submit_order(order)
        # Next submit succeeds
        bid = broker.submit_order(order)
        assert isinstance(bid, str)


# ─── Account & positions ──────────────────────────────────────────────────────


class TestAccountAndPositions:

    def test_buy_reduces_cash_and_creates_position(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.INSTANT, starting_cash=Decimal("10000"))
        broker.set_price("SPY", Decimal("450"))
        broker.register_event_callback(lambda _: None)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
        )
        broker.submit_order(order)
        acct = broker.get_account()
        assert acct.cash == Decimal("10000") - Decimal("4500")
        positions = broker.list_positions()
        assert len(positions) == 1
        assert positions[0].symbol == "SPY"
        assert positions[0].qty == Decimal("10")
        assert positions[0].avg_entry_price == Decimal("450")

    def test_sell_increases_cash_and_reduces_position(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.INSTANT, starting_cash=Decimal("10000"))
        broker.set_price("SPY", Decimal("450"))
        broker.register_event_callback(lambda _: None)
        # Build a position
        buy = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
        )
        broker.submit_order(buy)
        # Sell half
        broker.set_price("SPY", Decimal("500"))
        sell = make_market_order(
            symbol="SPY", side=OrderSide.SELL, qty=Decimal("5"), agent_id=AgentId.HAIKU,
        )
        broker.submit_order(sell)
        acct = broker.get_account()
        assert acct.cash == Decimal("10000") - Decimal("4500") + Decimal("2500")
        positions = broker.list_positions()
        assert len(positions) == 1
        assert positions[0].qty == Decimal("5")

    def test_full_close_removes_position(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.INSTANT, starting_cash=Decimal("10000"))
        broker.set_price("SPY", Decimal("450"))
        broker.register_event_callback(lambda _: None)
        buy = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
        )
        broker.submit_order(buy)
        sell = make_market_order(
            symbol="SPY", side=OrderSide.SELL, qty=Decimal("10"), agent_id=AgentId.HAIKU,
        )
        broker.submit_order(sell)
        assert broker.list_positions() == []


# ─── Open orders inspection ───────────────────────────────────────────────────


class TestOpenOrders:

    def test_open_orders_excludes_filled_and_cancelled(self) -> None:
        bc = _clock()
        broker = FakeBroker(clock=bc, fill_mode=FillMode.MANUAL)
        broker.set_price("SPY", Decimal("100"))
        broker.register_event_callback(lambda _: None)
        o1 = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        o2 = make_market_order(
            symbol="QQQ", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        broker.submit_order(o1)
        broker.submit_order(o2)
        assert len(broker.open_orders()) == 2
        broker.force_full_fill(o1.id)
        assert len(broker.open_orders()) == 1
        assert broker.open_orders()[0].symbol == "QQQ"
