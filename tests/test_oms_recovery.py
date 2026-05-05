"""Crash-recovery tests — the milestone-2 gate.

Per blueprint Principle 5 ("Append-only event log... persisted to SQLite WAL
*before* the side-effect"), the OMS must survive a crash at any point in the
order lifecycle without losing track of what's owed by the broker. These tests
prove every interesting failure point is non-fatal.

Crash simulation pattern: build OMS#1, drive it into the desired state, then
DROP THE INSTANCE without calling close() (mimicking SIGKILL). Construct
OMS#2 against the same SQLite file + same FakeBroker, call recover(), and
assert the recovered state matches reality.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from core.clock import BacktestClock
from core.events import EventBus
from core.types import AgentId, OrderSide, OrderState
from execution.broker import BrokerUnavailable
from execution.fake_broker import FakeBroker, FillMode, make_market_order
from execution.oms import OMS
from execution.oms_store import EventKind, OMSStore

# ─── Test scaffolding ─────────────────────────────────────────────────────────


def _new_clock() -> BacktestClock:
    return BacktestClock(datetime(2026, 4, 24, 14, 0, tzinfo=UTC))


def _crash_and_restart(
    db_path: Path,
    broker: FakeBroker,
    clock: BacktestClock,
) -> tuple[OMS, OMSStore, EventBus]:
    """Simulate a crash and bring up a new OMS against the same store + broker."""
    # NOTE: deliberately do NOT call store.close() on the old OMS — that's the crash.
    new_store = OMSStore(db_path)
    new_bus = EventBus()
    new_oms = OMS(broker=broker, store=new_store, bus=new_bus, clock=clock)
    return new_oms, new_store, new_bus


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 1: crash AFTER SUBMIT_INTENT but BEFORE the broker call
# ═════════════════════════════════════════════════════════════════════════════


class TestCrashBeforeBrokerCall:

    def test_broker_unavailable_then_recover_abandons(self, tmp_path: Path) -> None:
        """Broker is unavailable; we logged SUBMIT_INTENT but broker has no record.
        Recovery declares the order ABANDONED and marks it REJECTED."""
        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock)
        store1 = OMSStore(db)
        bus1 = EventBus()
        oms1 = OMS(broker=broker, store=store1, bus=bus1, clock=clock)

        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        broker.inject_submit_failure(BrokerUnavailable("simulated outage"))
        with contextlib.suppress(BrokerUnavailable):
            oms1.submit_order(order)

        # Crash. Broker still has no record.
        del oms1, store1
        oms2, store2, _bus2 = _crash_and_restart(db, broker, clock)
        summary = oms2.recover()

        assert summary.orders_replayed == 1
        assert summary.orders_abandoned == 1
        recovered = oms2.get_order(order.id)
        assert recovered is not None
        assert recovered.state == OrderState.REJECTED
        assert "abandoned" in (recovered.rejection_reason or "")

        # Audit trail in log
        kinds = [e.kind for e in store2.iter_for_order(order.id)]
        assert EventKind.RECONCILE_ABANDONED in kinds
        assert EventKind.ORDER_REJECTED in kinds


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 2: crash AFTER broker call, BEFORE ORDER_ACCEPTED logged
# ═════════════════════════════════════════════════════════════════════════════


class TestCrashAfterBrokerCallBeforeAcceptedLogged:

    def test_broker_has_order_recovery_backfills_accepted(self, tmp_path: Path) -> None:
        """Manually craft this race: SUBMIT_INTENT in log, broker has the order
        in ACCEPTED state, but no ORDER_ACCEPTED event made it to the log."""
        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock, fill_mode=FillMode.MANUAL)

        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("5"), agent_id=AgentId.SONNET,
        )

        # Manual setup: append SUBMIT_INTENT to store, then submit to broker bypassing OMS
        store_pre = OMSStore(db)
        from execution.oms import _serialize_order  # noqa: PLC0415
        store_pre.append(
            kind=EventKind.ORDER_SUBMIT_INTENT,
            order_id=order.id,
            payload=_serialize_order(order),
            ts=clock.now(),
        )
        # Broker now has the order with broker_order_id, ACCEPTED state
        broker_id = broker.submit_order(order)
        store_pre.close()

        # Restart
        oms2, store2, _bus2 = _crash_and_restart(db, broker, clock)
        summary = oms2.recover()

        assert summary.orders_recovered == 1
        recovered = oms2.get_order(order.id)
        assert recovered is not None
        assert recovered.state == OrderState.ACCEPTED
        assert recovered.broker_order_id == broker_id

        kinds = [e.kind for e in store2.iter_for_order(order.id)]
        assert EventKind.ORDER_ACCEPTED in kinds
        assert EventKind.RECONCILE_RECOVERED in kinds


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 3: crash AFTER ACCEPTED logged, broker still in ACCEPTED state
# ═════════════════════════════════════════════════════════════════════════════


class TestCrashAfterAcceptedNoFill:

    def test_broker_unchanged_recovery_is_noop(self, tmp_path: Path) -> None:
        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock, fill_mode=FillMode.MANUAL)
        store1 = OMSStore(db)
        bus1 = EventBus()
        oms1 = OMS(broker=broker, store=store1, bus=bus1, clock=clock)

        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        oms1.submit_order(order)
        assert oms1.get_order(order.id).state == OrderState.ACCEPTED
        del oms1, store1

        oms2, store2, _bus2 = _crash_and_restart(db, broker, clock)
        summary = oms2.recover()

        assert summary.orders_already_terminal == 0
        assert summary.orders_recovered == 0
        assert summary.orders_abandoned == 0

        recovered = oms2.get_order(order.id)
        assert recovered.state == OrderState.ACCEPTED  # unchanged

        kinds = [e.kind for e in store2.iter_for_order(order.id)]
        assert EventKind.RECONCILE_NOOP in kinds


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 4: crash AFTER ACCEPTED, broker fills during downtime
# ═════════════════════════════════════════════════════════════════════════════


class TestCrashThenBrokerFills:

    def test_full_fill_during_downtime_recovered(self, tmp_path: Path) -> None:
        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock, fill_mode=FillMode.MANUAL)
        broker.set_price("SPY", Decimal("450"))
        store1 = OMSStore(db)
        bus1 = EventBus()
        oms1 = OMS(broker=broker, store=store1, bus=bus1, clock=clock)

        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
        )
        oms1.submit_order(order)
        # Crash before any fill
        del oms1, store1

        # Broker fills the order while we're "down". The callback will fire
        # but no live OMS is registered to receive it — the fill is broker-side only.
        broker.register_event_callback(lambda _: None)  # discard any callbacks
        broker.force_full_fill(order.id, price=Decimal("452"))

        # Restart and recover
        oms2, store2, bus2 = _crash_and_restart(db, broker, clock)
        summary = oms2.recover()

        assert summary.orders_recovered == 1
        recovered = oms2.get_order(order.id)
        assert recovered.state == OrderState.FILLED
        assert recovered.filled_qty == Decimal("10")

        # A synthetic fill was generated
        fills = oms2.get_fills(order.id)
        assert len(fills) == 1
        assert fills[0].qty == Decimal("10")
        assert fills[0].price == Decimal("452")
        assert fills[0].agent_id == AgentId.HAIKU

    def test_partial_fill_received_then_full_fill_during_downtime(
        self, tmp_path: Path,
    ) -> None:
        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock, fill_mode=FillMode.MANUAL)
        broker.set_price("SPY", Decimal("450"))
        store1 = OMSStore(db)
        bus1 = EventBus()
        oms1 = OMS(broker=broker, store=store1, bus=bus1, clock=clock)

        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
        )
        oms1.submit_order(order)
        broker.force_partial_fill(order.id, qty=Decimal("3"), price=Decimal("450"))
        assert oms1.get_order(order.id).filled_qty == Decimal("3")
        # Crash now
        del oms1, store1

        # Broker fills the remaining 7 while we're down
        broker.register_event_callback(lambda _: None)
        broker.force_partial_fill(order.id, qty=Decimal("7"), price=Decimal("455"))

        oms2, _store2, _bus2 = _crash_and_restart(db, broker, clock)
        summary = oms2.recover()

        assert summary.orders_recovered == 1
        recovered = oms2.get_order(order.id)
        assert recovered.state == OrderState.FILLED
        assert recovered.filled_qty == Decimal("10")

        # Fills: 1 from before crash (qty=3) + 1 synthetic from recovery (qty=7) = 2
        fills = oms2.get_fills(order.id)
        assert len(fills) == 2
        assert sum((f.qty for f in fills), Decimal("0")) == Decimal("10")


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 5: terminal-state orders survive recovery unchanged
# ═════════════════════════════════════════════════════════════════════════════


class TestTerminalStatesPreserved:

    def test_stuck_partial_with_broker_filled_force_closes(self, tmp_path: Path) -> None:
        """Reproduces the production reconcile-churn bug: local order is stuck
        non-terminal but broker reports FILLED with matching filled_qty
        (delta_qty <= 0). Pre-fix, _catch_up_from_broker emitted
        RECONCILE_RECOVERED without advancing local state, so the same orders
        re-replayed every restart (155 noise events/day in observed data).
        Post-fix: force-close locally + emit a single RECOVERED event;
        subsequent passes are true RECONCILE_NOOPs."""
        from execution.broker import BrokerOrderState, BrokerOrderStatus

        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock, fill_mode=FillMode.MANUAL)
        broker.set_price("SPY", Decimal("450"))
        store = OMSStore(db)
        bus = EventBus()
        oms = OMS(broker=broker, store=store, bus=bus, clock=clock)

        # Use sub-1e-9 precision in order.qty so a clean 10-share fill leaves
        # is_full=False (residual >= 1e-9), keeping FSM in PARTIAL with
        # local.filled_qty == broker.filled_qty == 10. This is the same
        # mechanism that produced 31 stuck orders in production.
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY,
            qty=Decimal("10.00000001"), agent_id=AgentId.HAIKU,
        )
        oms.submit_order(order)
        broker.force_partial_fill(order.id, qty=Decimal("10"), price=Decimal("450"))
        assert oms.get_order(order.id).state == OrderState.PARTIAL
        assert oms.get_order(order.id).filled_qty == Decimal("10")

        broker_status = BrokerOrderStatus(
            broker_order_id=oms._orders[order.id].broker_order_id or "stub",
            client_order_id=order.id,
            symbol=order.symbol,
            side=order.side,
            qty=order.qty,
            filled_qty=Decimal("10"),
            avg_fill_price=Decimal("450"),
            state=BrokerOrderState.FILLED,
            submitted_at=clock.now(),
            updated_at=clock.now(),
        )

        # First call: should force-close + emit RECOVERED.
        before_recovered = sum(
            1 for e in store.iter_all()
            if e.kind == EventKind.RECONCILE_RECOVERED and e.order_id == order.id
        )
        oms._catch_up_from_broker(order.id, broker_status)
        assert oms.get_order(order.id).state == OrderState.FILLED
        after_first = sum(
            1 for e in store.iter_all()
            if e.kind == EventKind.RECONCILE_RECOVERED and e.order_id == order.id
        )
        assert after_first == before_recovered + 1

        # Second call: state is already terminal — _reconcile_open_orders skips.
        # Calling _catch_up_from_broker directly is the worst case; verify it
        # emits NOOP, not another RECOVERED.
        oms._catch_up_from_broker(order.id, broker_status)
        after_second_recovered = sum(
            1 for e in store.iter_all()
            if e.kind == EventKind.RECONCILE_RECOVERED and e.order_id == order.id
        )
        noop_events = sum(
            1 for e in store.iter_all()
            if e.kind == EventKind.RECONCILE_NOOP and e.order_id == order.id
        )
        assert after_second_recovered == after_first  # no new RECOVERED
        assert noop_events >= 1                       # second pass was a NOOP

    def test_filled_order_stays_filled_after_recovery(self, tmp_path: Path) -> None:
        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock, fill_mode=FillMode.INSTANT)
        broker.set_price("SPY", Decimal("450"))
        store1 = OMSStore(db)
        bus1 = EventBus()
        oms1 = OMS(broker=broker, store=store1, bus=bus1, clock=clock)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        oms1.submit_order(order)
        del oms1, store1

        oms2, _store2, _bus2 = _crash_and_restart(db, broker, clock)
        summary = oms2.recover()
        assert summary.orders_already_terminal == 1
        assert oms2.get_order(order.id).state == OrderState.FILLED

    def test_rejected_order_stays_rejected_after_recovery(self, tmp_path: Path) -> None:
        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock, fill_mode=FillMode.REJECT)
        store1 = OMSStore(db)
        bus1 = EventBus()
        oms1 = OMS(broker=broker, store=store1, bus=bus1, clock=clock)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        oms1.submit_order(order)
        del oms1, store1

        oms2, _store2, _bus2 = _crash_and_restart(db, broker, clock)
        summary = oms2.recover()
        assert summary.orders_already_terminal == 1
        assert oms2.get_order(order.id).state == OrderState.REJECTED

    def test_cancelled_order_stays_cancelled_after_recovery(self, tmp_path: Path) -> None:
        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock, fill_mode=FillMode.MANUAL)
        store1 = OMSStore(db)
        bus1 = EventBus()
        oms1 = OMS(broker=broker, store=store1, bus=bus1, clock=clock)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        oms1.submit_order(order)
        oms1.cancel_order(order.id)
        del oms1, store1

        oms2, _store2, _bus2 = _crash_and_restart(db, broker, clock)
        summary = oms2.recover()
        assert summary.orders_already_terminal == 1
        assert oms2.get_order(order.id).state == OrderState.CANCELLED


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 6: many orders, mixed states, all survive
# ═════════════════════════════════════════════════════════════════════════════


class TestManyOrdersMixedStates:

    def test_replay_preserves_state_for_all(self, tmp_path: Path) -> None:
        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock, fill_mode=FillMode.MANUAL)
        broker.set_price("SPY", Decimal("450"))
        broker.set_price("QQQ", Decimal("400"))
        store1 = OMSStore(db)
        bus1 = EventBus()
        oms1 = OMS(broker=broker, store=store1, bus=bus1, clock=clock)

        # Order A: filled
        a = make_market_order(symbol="SPY", side=OrderSide.BUY,
                              qty=Decimal("1"), agent_id=AgentId.HAIKU)
        oms1.submit_order(a)
        broker.force_full_fill(a.id)

        # Order B: partial then crash
        b = make_market_order(symbol="QQQ", side=OrderSide.BUY,
                              qty=Decimal("10"), agent_id=AgentId.SONNET)
        oms1.submit_order(b)
        broker.force_partial_fill(b.id, qty=Decimal("4"))

        # Order C: open (ACCEPTED, no fills)
        c = make_market_order(symbol="SPY", side=OrderSide.BUY,
                              qty=Decimal("2"), agent_id=AgentId.OPUS)
        oms1.submit_order(c)

        # Order D: cancelled
        d = make_market_order(symbol="QQQ", side=OrderSide.BUY,
                              qty=Decimal("1"), agent_id=AgentId.HAIKU)
        oms1.submit_order(d)
        oms1.cancel_order(d.id)

        del oms1, store1

        # In the crash window, broker finishes filling B and fills C
        broker.register_event_callback(lambda _: None)
        broker.force_full_fill(b.id, price=Decimal("405"))
        broker.force_full_fill(c.id, price=Decimal("455"))

        oms2, _store2, _bus2 = _crash_and_restart(db, broker, clock)
        summary = oms2.recover()
        # A and D were terminal pre-crash; B and C needed reconciliation
        assert summary.orders_already_terminal == 2  # A (FILLED), D (CANCELLED)
        assert summary.orders_recovered == 2          # B and C

        assert oms2.get_order(a.id).state == OrderState.FILLED
        assert oms2.get_order(b.id).state == OrderState.FILLED
        assert oms2.get_order(b.id).filled_qty == Decimal("10")
        assert oms2.get_order(c.id).state == OrderState.FILLED
        assert oms2.get_order(d.id).state == OrderState.CANCELLED


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 7: idempotency and post-recovery functionality
# ═════════════════════════════════════════════════════════════════════════════


class TestPostRecovery:

    def test_recover_twice_is_idempotent(self, tmp_path: Path) -> None:
        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock, fill_mode=FillMode.INSTANT)
        broker.set_price("SPY", Decimal("450"))
        store1 = OMSStore(db)
        bus1 = EventBus()
        oms1 = OMS(broker=broker, store=store1, bus=bus1, clock=clock)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        oms1.submit_order(order)
        del oms1, store1

        oms2, store2, _bus2 = _crash_and_restart(db, broker, clock)
        oms2.recover()
        s2 = oms2.recover()
        # Second recover sees terminal-only orders (no work to do)
        assert s2.orders_recovered == 0
        assert s2.orders_abandoned == 0
        assert oms2.get_order(order.id).state == OrderState.FILLED

    def test_recovered_oms_can_submit_new_orders(self, tmp_path: Path) -> None:
        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock, fill_mode=FillMode.INSTANT)
        broker.set_price("SPY", Decimal("450"))
        broker.set_price("QQQ", Decimal("400"))

        # Pre-existing order in the log
        store1 = OMSStore(db)
        bus1 = EventBus()
        oms1 = OMS(broker=broker, store=store1, bus=bus1, clock=clock)
        prior = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        oms1.submit_order(prior)
        del oms1, store1

        # Restart, recover, then submit a fresh order
        oms2, store2, _bus2 = _crash_and_restart(db, broker, clock)
        oms2.recover()

        new_order = make_market_order(
            symbol="QQQ", side=OrderSide.BUY, qty=Decimal("2"), agent_id=AgentId.SONNET,
        )
        result = oms2.submit_order(new_order)
        assert result.accepted is True
        assert oms2.get_order(new_order.id).state == OrderState.FILLED
        # Both orders coexist
        assert oms2.get_order(prior.id).state == OrderState.FILLED

    def test_idempotent_broker_resubmit_after_recovery(self, tmp_path: Path) -> None:
        """If we resubmit the same client_order_id post-recovery, the broker
        returns the same broker_order_id rather than creating a duplicate."""
        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock, fill_mode=FillMode.MANUAL)
        store1 = OMSStore(db)
        bus1 = EventBus()
        oms1 = OMS(broker=broker, store=store1, bus=bus1, clock=clock)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("1"), agent_id=AgentId.HAIKU,
        )
        oms1.submit_order(order)
        broker_id_1 = oms1.get_order(order.id).broker_order_id
        del oms1, store1

        oms2, _store2, _bus2 = _crash_and_restart(db, broker, clock)
        oms2.recover()
        # Try to "resubmit" — actually this raises because the local order is
        # ACCEPTED, not PENDING. But the broker idempotency contract is what
        # protects us in the abandoned-then-retry case.
        from dataclasses import replace as dr_replace  # noqa: PLC0415
        retry = dr_replace(order, state=OrderState.PENDING)
        bid = broker.submit_order(retry)
        assert bid == broker_id_1   # same broker_order_id


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 8: empty database recovery
# ═════════════════════════════════════════════════════════════════════════════


class TestEmptyRecovery:

    def test_recover_on_empty_log_is_noop(self, tmp_path: Path) -> None:
        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock)
        store = OMSStore(db)
        bus = EventBus()
        oms = OMS(broker=broker, store=store, bus=bus, clock=clock)
        summary = oms.recover()
        assert summary.orders_replayed == 0
        assert summary.orders_recovered == 0
        assert summary.orders_abandoned == 0
        assert summary.orders_already_terminal == 0
        assert oms.list_orders() == []


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 9: position consistency after recovery (the real-money invariant)
# ═════════════════════════════════════════════════════════════════════════════


class TestPositionConsistency:

    def test_local_position_view_matches_broker_after_recovery(
        self, tmp_path: Path,
    ) -> None:
        """The single most important property: after recovery, our derived
        position state agrees with the broker's. This is what reconciliation
        is for."""
        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock, fill_mode=FillMode.MANUAL,
                            starting_cash=Decimal("100000"))
        broker.set_price("SPY", Decimal("450"))
        store1 = OMSStore(db)
        bus1 = EventBus()
        oms1 = OMS(broker=broker, store=store1, bus=bus1, clock=clock)

        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
        )
        oms1.submit_order(order)
        del oms1, store1

        # Broker fills during crash
        broker.register_event_callback(lambda _: None)
        broker.force_full_fill(order.id, price=Decimal("450"))

        # Recover
        oms2, _store2, _bus2 = _crash_and_restart(db, broker, clock)
        oms2.recover()

        # Aggregate filled qty per symbol from our recovered fills
        local_position_qty = sum(
            (f.qty for f in oms2.get_fills(order.id)),
            start=Decimal("0"),
        )
        broker_positions = {p.symbol: p.qty for p in broker.list_positions()}
        assert local_position_qty == broker_positions["SPY"]


# ═════════════════════════════════════════════════════════════════════════════
# Scenario 10: replay only (no broker reconciliation needed)
# ═════════════════════════════════════════════════════════════════════════════


class TestReplayCorrectness:

    def test_replay_reconstructs_avg_fill_price_correctly(self, tmp_path: Path) -> None:
        db = tmp_path / "oms.db"
        clock = _new_clock()
        broker = FakeBroker(clock=clock, fill_mode=FillMode.MANUAL)
        store1 = OMSStore(db)
        bus1 = EventBus()
        oms1 = OMS(broker=broker, store=store1, bus=bus1, clock=clock)

        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("9"), agent_id=AgentId.HAIKU,
        )
        oms1.submit_order(order)
        broker.force_partial_fill(order.id, qty=Decimal("3"), price=Decimal("100"))
        broker.force_partial_fill(order.id, qty=Decimal("3"), price=Decimal("110"))
        broker.force_partial_fill(order.id, qty=Decimal("3"), price=Decimal("120"))

        pre_avg = oms1.get_order(order.id).filled_avg_price
        del oms1, store1

        oms2, _store2, _bus2 = _crash_and_restart(db, broker, clock)
        oms2.recover()
        post_avg = oms2.get_order(order.id).filled_avg_price
        assert pre_avg == post_avg
        # Verified: (3*100 + 3*110 + 3*120) / 9 = 110
        assert post_avg == Decimal("110")
