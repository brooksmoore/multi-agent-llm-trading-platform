"""Tests for execution/reconciler.py — periodic OMS ↔ broker reconciliation.

Uses FakeBroker so no real Alpaca connection is required.
OMS is wired to FakeBroker exactly as in production.
"""

from __future__ import annotations

from dataclasses import replace as dc_replace
from datetime import UTC, datetime
from decimal import Decimal

from core.events import EventBus
from core.types import AgentId, OrderSide, OrderState
from execution.broker import BrokerOrderState
from execution.fake_broker import FakeBroker, FillMode, make_market_order
from execution.kill_switch import KillSwitchEngine
from execution.oms import OMS
from execution.oms_store import OMSStore
from execution.reconciler import Reconciler

# ── Helpers ───────────────────────────────────────────────────────────────────

_TS = datetime(2026, 4, 24, 10, 0, tzinfo=UTC)


def _setup() -> tuple[Reconciler, OMS, FakeBroker, KillSwitchEngine]:
    broker = FakeBroker()
    store = OMSStore(":memory:")
    bus = EventBus()
    oms = OMS(broker, store, bus)
    kill = KillSwitchEngine()
    rec = Reconciler(oms, broker, kill, interval_secs=1)
    return rec, oms, broker, kill


# ── reconcile_once basic ──────────────────────────────────────────────────────


def test_reconcile_empty_oms_returns_zeros() -> None:
    rec, oms, broker, kill = _setup()
    result = rec.reconcile_once(_TS)
    assert result.orders_checked == 0
    assert result.orders_updated == 0
    assert result.position_mismatches == 0
    assert result.kill_switch_tripped is False


def test_reconcile_no_mismatch_does_not_trip_kill_switch() -> None:
    rec, oms, broker, kill = _setup()
    # Submit + fill an order so both OMS and broker agree on a position
    order = make_market_order(
        symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
    )
    oms.submit_order(order)   # FakeBroker INSTANT mode fills immediately
    result = rec.reconcile_once(_TS)
    assert result.kill_switch_tripped is False


def test_reconcile_position_mismatch_trips_kill_switch() -> None:
    rec, oms, broker, kill = _setup()

    # OMS thinks we have 10 SPY (via a fill), but broker has nothing (we'll clear it)
    order = make_market_order(
        symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
    )
    oms.submit_order(order)  # OMS and broker both record the fill

    # Manually wipe the broker's position to simulate a mismatch
    broker._positions.clear()  # noqa: SLF001

    result = rec.reconcile_once(_TS)
    assert result.position_mismatches >= 1
    assert result.kill_switch_tripped is True
    from core.types import KillSwitchState  # noqa: PLC0415
    assert kill.state == KillSwitchState.RECONCILIATION_BREAK


def test_reconcile_within_tolerance_no_trip() -> None:
    """Fractional qty below 1-share tolerance does not trip the kill switch."""
    rec, oms, broker, kill = _setup()

    order = make_market_order(
        symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
    )
    oms.submit_order(order)

    # Artificially adjust broker position by 0.5 shares (within 1-share tolerance)
    pos = broker._positions["SPY"]  # noqa: SLF001
    broker._positions["SPY"] = dc_replace(pos, qty=Decimal("9.5"))  # noqa: SLF001

    result = rec.reconcile_once(_TS)
    assert result.kill_switch_tripped is False


# ── order reconciliation ──────────────────────────────────────────────────────


def test_reconcile_open_order_checks_broker_status() -> None:
    """Broker shows CANCELLED while OMS still shows ACCEPTED — reconciler syncs OMS."""
    broker = FakeBroker(fill_mode=FillMode.MANUAL)
    store = OMSStore(":memory:")
    bus = EventBus()
    oms = OMS(broker, store, bus)
    kill = KillSwitchEngine()
    rec = Reconciler(oms, broker, kill)

    order = make_market_order(
        symbol="SPY", side=OrderSide.BUY, qty=Decimal("5"), agent_id=AgentId.HAIKU,
    )
    result = oms.submit_order(order)
    assert result.accepted is True

    # OMS thinks the order is still ACCEPTED; fake a broker-side cancel without
    # emitting the callback (simulates a scenario where the stream was disconnected).
    broker_id = result.order.broker_order_id
    assert broker_id is not None
    with broker._lock:  # noqa: SLF001
        rec_entry = broker._orders[broker_id]  # noqa: SLF001
        rec_entry.status = dc_replace(rec_entry.status, state=BrokerOrderState.CANCELED)

    # Now reconcile: should detect the broker's CANCELLED status and update OMS.
    rec_result = rec.reconcile_once(_TS)
    assert rec_result.orders_updated >= 1

    updated_order = oms.get_order(order.id)
    assert updated_order is not None
    assert updated_order.state == OrderState.CANCELLED


# ── start / stop ──────────────────────────────────────────────────────────────


def test_start_stop_background_thread() -> None:
    rec, oms, broker, kill = _setup()
    rec.start()
    assert rec._thread is not None  # noqa: SLF001
    assert rec._thread.is_alive()   # noqa: SLF001
    rec.stop()
    assert not rec._thread.is_alive() if rec._thread else True


def test_start_is_idempotent() -> None:
    rec, oms, broker, kill = _setup()
    rec.start()
    first_thread = rec._thread  # noqa: SLF001
    rec.start()  # second call should not create a new thread
    assert rec._thread is first_thread  # noqa: SLF001
    rec.stop()


# ── multiple symbols ──────────────────────────────────────────────────────────


def test_reconcile_multiple_symbols_all_match() -> None:
    rec, oms, broker, kill = _setup()
    for symbol in ("SPY", "QQQ", "IWM"):
        order = make_market_order(
            symbol=symbol, side=OrderSide.BUY, qty=Decimal("5"), agent_id=AgentId.HAIKU,
        )
        oms.submit_order(order)
    result = rec.reconcile_once(_TS)
    assert result.kill_switch_tripped is False


def test_reconcile_sell_reduces_expected_position() -> None:
    rec, oms, broker, kill = _setup()

    buy = make_market_order(
        symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
    )
    oms.submit_order(buy)

    sell = make_market_order(
        symbol="SPY", side=OrderSide.SELL, qty=Decimal("10"), agent_id=AgentId.HAIKU,
    )
    oms.submit_order(sell)

    # OMS and broker should both show 0 SPY — no mismatch
    result = rec.reconcile_once(_TS)
    assert result.kill_switch_tripped is False
