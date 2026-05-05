"""Order Management System.

Owns the trade lifecycle. Every state transition follows this rule:

    1. PERSIST event to the append-only log (durable)
    2. UPDATE in-memory state (FSM + Order snapshot)
    3. PUBLISH event on the EventBus for dashboard / journal / etc.

If we crash anywhere, the log is the truth. On restart, `recover()` replays
the log and reconciles any non-terminal orders against the broker (broker is
the source of truth per blueprint Principle 4).

Idempotency: every broker submission carries `client_order_id = order.id`.
The Broker contract guarantees identical client_order_id → identical
broker_order_id, so retrying a submit after a crash never creates a
duplicate position.

Threading: a re-entrant lock serializes every state mutation. The broker
callback may arrive in another thread (websocket) or synchronously inside
submit_order (FakeBroker INSTANT mode); RLock handles both safely.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any

from core.clock import Clock, WallClock
from core.events import (
    EventBus,
    FillReceivedEvent,
    OrderPlacedEvent,
    OrderStateChangedEvent,
)
from core.state_machine import StateMachine, build_order_fsm
from core.types import (
    AgentId,
    Fill,
    Order,
    OrderClass,
    OrderEvent,
    OrderId,
    OrderSide,
    OrderState,
    OrderType,
    TimeInForce,
    new_id,
)
from execution.broker import (
    Broker,
    BrokerError,
    BrokerOrderEvent,
    BrokerOrderState,
    BrokerOrderStatus,
    BrokerRejection,
    BrokerUnavailable,
)
from execution.oms_store import EventKind, OMSStore

logger = logging.getLogger(__name__)


# ─── Result types ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SubmitResult:
    order: Order
    broker_order_id: str | None
    accepted: bool
    rejection_reason: str | None = None


@dataclass(frozen=True)
class ReconcileSummary:
    """Returned by recover() so callers can see what reconciliation did."""

    orders_replayed: int
    orders_recovered: int    # we backfilled state from broker
    orders_abandoned: int    # broker had no record; declared lost
    orders_already_terminal: int


# ─── OMS ──────────────────────────────────────────────────────────────────────


class OMS:
    """Order Management System.

    Construction does not implicitly recover; call `recover()` after wiring
    so callers can choose to skip recovery for fresh databases.
    """

    def __init__(
        self,
        broker: Broker,
        store: OMSStore,
        bus: EventBus,
        clock: Clock | None = None,
    ) -> None:
        self._broker = broker
        self._store = store
        self._bus = bus
        self._clock: Clock = clock if clock is not None else WallClock()

        self._lock = threading.RLock()
        self._orders: dict[OrderId, Order] = {}
        self._fsms: dict[OrderId, StateMachine[OrderState, OrderEvent]] = {}
        self._broker_id_to_order: dict[str, OrderId] = {}
        self._fills_by_order: dict[OrderId, list[Fill]] = {}

        # Wire broker callbacks
        self._broker.register_event_callback(self._on_broker_event)

    # ─── Public API ───────────────────────────────────────────────────────────

    def submit_order(self, order: Order) -> SubmitResult:
        """Submit `order` to the broker.

        Sequence:
            1. Persist ORDER_SUBMIT_INTENT (Order snapshot serialized)
            2. Trigger FSM SUBMIT (PENDING → SUBMITTED)
            3. Call broker.submit_order
            4a. On accept: callback handler persists ORDER_ACCEPTED + transitions
            4b. On BrokerRejection: persist ORDER_REJECTED + transition
            4c. On BrokerUnavailable: re-raise (caller may retry; client_order_id is idempotent)
        """
        if order.state != OrderState.PENDING:
            raise ValueError(f"submit_order requires PENDING state, got {order.state}")

        with self._lock:
            now = self._clock.now()
            self._orders[order.id] = order
            self._fsms[order.id] = build_order_fsm(OrderState.PENDING)
            self._fills_by_order[order.id] = []

            # 1. Persist intent BEFORE side effect
            self._store.append(
                kind=EventKind.ORDER_SUBMIT_INTENT,
                order_id=order.id,
                payload=_serialize_order(order),
                ts=now,
            )

            # 2. Update FSM and snapshot to SUBMITTED
            self._transition(order.id, OrderEvent.SUBMIT, ts=now)

            # Publish OrderPlacedEvent (the agent / dashboard cares about this)
            self._bus.publish(OrderPlacedEvent(order=self._orders[order.id]))

            # 3. Call broker (may fire callback synchronously for INSTANT mode)
            try:
                broker_order_id = self._broker.submit_order(order)
            except BrokerRejection as e:
                reason = str(e)
                self._handle_rejection(order.id, reason=reason, ts=self._clock.now())
                return SubmitResult(
                    order=self._orders[order.id],
                    broker_order_id=None,
                    accepted=False,
                    rejection_reason=reason,
                )
            except BrokerUnavailable:
                # Don't transition; caller can retry. The log shows SUBMIT_INTENT
                # without ACCEPT/REJECT, and recover() will reconcile if needed.
                logger.exception("Broker unavailable for order %s; caller may retry", order.id)
                raise

            # If callback already recorded ORDER_ACCEPTED (FakeBroker INSTANT path),
            # _broker_id_to_order[broker_order_id] already maps. Idempotent.
            if broker_order_id not in self._broker_id_to_order:
                self._record_accepted(order.id, broker_order_id, ts=self._clock.now())

            return SubmitResult(
                order=self._orders[order.id],
                broker_order_id=broker_order_id,
                accepted=True,
            )

    def cancel_order(self, order_id: OrderId) -> None:
        """Request cancellation of an open order."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise KeyError(f"Unknown order_id={order_id}")
            if order.state in (
                OrderState.FILLED,
                OrderState.CANCELLED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
            ):
                logger.debug("cancel_order: %s already terminal (%s)", order_id, order.state)
                return
            if order.broker_order_id is None:
                # Can't ask the broker to cancel something it never accepted.
                # Move locally to CANCELLED.
                self._store.append(
                    kind=EventKind.ORDER_CANCELLED,
                    order_id=order_id,
                    payload={"reason": "cancel before broker accept"},
                    ts=self._clock.now(),
                )
                self._transition(order_id, OrderEvent.CANCEL, ts=self._clock.now())
                return

            self._store.append(
                kind=EventKind.ORDER_CANCEL_REQUESTED,
                order_id=order_id,
                payload={"broker_order_id": order.broker_order_id},
                ts=self._clock.now(),
            )
            try:
                self._broker.cancel_order(order.broker_order_id)
            except BrokerError:
                logger.exception("cancel_order broker call failed for %s", order_id)
                raise
            # Broker will emit CANCELED via callback; that's where we transition.

    # ─── Inspection ───────────────────────────────────────────────────────────

    def get_order(self, order_id: OrderId) -> Order | None:
        with self._lock:
            return self._orders.get(order_id)

    def list_orders(self) -> list[Order]:
        with self._lock:
            return list(self._orders.values())

    def list_open_orders(self) -> list[Order]:
        with self._lock:
            return [
                o for o in self._orders.values()
                if o.state in (
                    OrderState.PENDING,
                    OrderState.SUBMITTED,
                    OrderState.ACCEPTED,
                    OrderState.PARTIAL,
                )
            ]

    def get_fills(self, order_id: OrderId) -> list[Fill]:
        with self._lock:
            return list(self._fills_by_order.get(order_id, []))

    def on_broker_event(self, event: BrokerOrderEvent) -> None:
        """Public entry point for broker events (used by Reconciler and tests).

        Same logic as the internal callback registered with the broker at
        construction time. Thread-safe.
        """
        self._on_broker_event(event)

    def adopt_orphan_position(
        self,
        symbol: str,
        qty: Decimal,
        price: Decimal,
        agent_id: AgentId,
        ts: datetime,
    ) -> None:
        """Inject a synthetic fill for a broker position the OMS missed.

        Used by the reconciler when the broker holds a position that has no
        matching OMS record (e.g. fills lost during a websocket outage). Creates
        a ghost order + fill so _compute_expected_positions() agrees with the
        broker on the next reconcile pass. Idempotent on restart: the persisted
        events are replayed by recover().
        """
        ghost_order_id = new_id()
        ghost_broker_id = f"ghost-{ghost_order_id}"

        ghost_order = Order(
            id=ghost_order_id,
            intent_id=new_id(),
            agent_id=agent_id,
            symbol=symbol,
            side=OrderSide.BUY,
            qty=qty,
            order_type=OrderType.MARKET,
            order_class=OrderClass.SIMPLE,
            time_in_force=TimeInForce.GTC,
            state=OrderState.PENDING,
            created_at=ts,
        )
        fill = Fill(
            id=new_id(),
            order_id=ghost_order_id,
            agent_id=agent_id,
            symbol=symbol,
            side=OrderSide.BUY,
            qty=qty,
            price=price,
            timestamp=ts,
            commission=Decimal("0"),
            is_partial=False,
        )

        with self._lock:
            # Set up in-memory order state (PENDING → SUBMITTED → ACCEPTED)
            self._orders[ghost_order_id] = ghost_order
            self._fsms[ghost_order_id] = build_order_fsm(OrderState.PENDING)
            self._fills_by_order[ghost_order_id] = []

            self._store.append(EventKind.ORDER_SUBMIT_INTENT, ghost_order_id,
                               _serialize_order(ghost_order), ts)
            self._fsms[ghost_order_id].trigger(OrderEvent.SUBMIT)
            self._orders[ghost_order_id] = replace(
                self._orders[ghost_order_id], state=OrderState.SUBMITTED)

            self._store.append(EventKind.ORDER_ACCEPTED, ghost_order_id,
                               {"broker_order_id": ghost_broker_id}, ts)
            self._broker_id_to_order[ghost_broker_id] = ghost_order_id
            self._orders[ghost_order_id] = replace(
                self._orders[ghost_order_id],
                broker_order_id=ghost_broker_id,
                submitted_at=ts,
            )
            self._fsms[ghost_order_id].trigger(OrderEvent.ACCEPT)
            self._orders[ghost_order_id] = replace(
                self._orders[ghost_order_id], state=OrderState.ACCEPTED)

            # Apply fill — drives FSM to FILLED and publishes FillReceivedEvent
            self._handle_fill(ghost_order_id, fill, ts=ts)

        logger.info(
            "OMS: adopted orphan position %s qty=%.6f @ %.4f for agent=%s",
            symbol, qty, price, agent_id,
        )

    # ─── Recovery ─────────────────────────────────────────────────────────────

    def recover(self) -> ReconcileSummary:
        """Replay the log and reconcile non-terminal orders against the broker.

        MUST be called after construction if the database may contain prior events.
        Safe to call on an empty database (no-op).
        """
        with self._lock:
            self._replay_log()
            return self._reconcile_open_orders()

    # ─── Broker callback ──────────────────────────────────────────────────────

    def _on_broker_event(self, event: BrokerOrderEvent) -> None:
        """Handle async event from the broker.

        FakeBroker INSTANT mode delivers this synchronously inside submit_order;
        real Alpaca delivers via websocket thread. RLock supports both.
        """
        with self._lock:
            order_id = event.client_order_id
            order = self._orders.get(order_id)
            if order is None:
                # Could happen during recovery if broker emits stale events for
                # an order we don't know about yet. Log and ignore.
                logger.warning(
                    "Broker event for unknown order_id=%s, broker_id=%s, state=%s",
                    order_id, event.broker_order_id, event.new_state,
                )
                return

            ts = event.timestamp if event.timestamp is not None else self._clock.now()

            match event.new_state:
                case BrokerOrderState.ACCEPTED:
                    if order_id not in self._broker_id_to_order_inverse():
                        self._record_accepted(order_id, event.broker_order_id, ts=ts)
                case BrokerOrderState.PARTIALLY_FILLED | BrokerOrderState.FILLED:
                    if event.fill is None:
                        logger.error("FILL event without fill payload: %s", event)
                        return
                    self._handle_fill(order_id, event.fill, ts=ts)
                case BrokerOrderState.CANCELED:
                    self._handle_cancellation(order_id, ts=ts)
                case BrokerOrderState.REJECTED:
                    self._handle_rejection(
                        order_id,
                        reason=event.rejection_reason or "broker rejected",
                        ts=ts,
                    )
                case BrokerOrderState.EXPIRED:
                    self._handle_expiry(order_id, ts=ts)
                case BrokerOrderState.NEW:
                    # NEW just means broker received it but hasn't routed yet.
                    # We treat ACCEPTED as the meaningful transition; ignore NEW.
                    pass
                case BrokerOrderState.UNKNOWN:
                    logger.warning("Broker reported UNKNOWN state for %s", order_id)

    # ─── Internal: state transitions (caller MUST hold self._lock) ───────────

    def _record_accepted(self, order_id: OrderId, broker_order_id: str, ts: datetime) -> None:
        order = self._orders[order_id]
        if order.broker_order_id == broker_order_id:
            # Idempotent: callback fired twice
            return
        self._store.append(
            kind=EventKind.ORDER_ACCEPTED,
            order_id=order_id,
            payload={"broker_order_id": broker_order_id},
            ts=ts,
        )
        self._broker_id_to_order[broker_order_id] = order_id
        # Update Order snapshot: set broker_order_id + submitted_at
        self._orders[order_id] = replace(
            order,
            broker_order_id=broker_order_id,
            submitted_at=ts,
        )
        self._transition(order_id, OrderEvent.ACCEPT, ts=ts)

    def _handle_fill(self, order_id: OrderId, fill: Fill, ts: datetime) -> None:
        # Persist the fill; agent_id on the fill comes from the broker payload.
        # If FakeBroker stamped the wrong agent_id (legacy), correct it from our Order.
        order = self._orders[order_id]
        if fill.agent_id != order.agent_id:
            fill = replace(fill, agent_id=order.agent_id)
        # Canonicalize symbol so the lots ledger and downstream consumers see
        # one key per logical position even when the broker echoes the slashed
        # crypto form (BTC/USD) for an order we submitted as BTCUSD.
        if fill.symbol != order.symbol:
            fill = replace(fill, symbol=order.symbol)

        self._store.append(
            kind=EventKind.FILL_RECEIVED,
            order_id=order_id,
            payload=_serialize_fill(fill),
            ts=ts,
        )
        self._fills_by_order[order_id].append(fill)

        # Update aggregated Order snapshot
        new_filled_qty = order.filled_qty + fill.qty
        # Tolerance: brokers round fill qty (typically 9dp) while the order qty
        # may carry more precision from sizing math. Treat as fully filled when
        # the residual is below 1e-9 to avoid orders stuck in PARTIALLY_FILLED.
        is_full = (order.qty - new_filled_qty) < Decimal("1e-9")
        if order.filled_avg_price is None:
            new_avg = fill.price
        else:
            total_value = order.filled_avg_price * order.filled_qty + fill.price * fill.qty
            new_avg = total_value / new_filled_qty
        self._orders[order_id] = replace(
            order,
            filled_qty=new_filled_qty,
            filled_avg_price=new_avg,
            filled_at=ts if is_full else order.filled_at,
        )

        # FSM transition
        if is_full:
            self._transition(order_id, OrderEvent.FULL_FILL, ts=ts)
        else:
            self._transition(order_id, OrderEvent.PARTIAL_FILL, ts=ts)

        # Publish FillReceivedEvent (dashboard / journal / lots ledger)
        self._bus.publish(FillReceivedEvent(fill=fill))

    def force_close_filled(self, order_id: OrderId, ts: datetime) -> None:
        """Force-transition an order to FILLED when broker confirms terminal
        FILLED but local fills already cover the recorded broker qty (typically
        precision drift between order.qty and rounded fill qtys).
        Idempotent: no-op if order is already terminal."""
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                return
            if order.state in (
                OrderState.FILLED, OrderState.CANCELLED,
                OrderState.REJECTED, OrderState.EXPIRED,
            ):
                return
            self._orders[order_id] = replace(
                order, qty=order.filled_qty, filled_at=ts,
            )
            self._transition(order_id, OrderEvent.FULL_FILL, ts=ts)

    def _handle_cancellation(self, order_id: OrderId, ts: datetime) -> None:
        if self._orders[order_id].state in (
            OrderState.CANCELLED, OrderState.FILLED, OrderState.REJECTED, OrderState.EXPIRED,
        ):
            return
        self._store.append(
            kind=EventKind.ORDER_CANCELLED,
            order_id=order_id,
            payload={},
            ts=ts,
        )
        self._transition(order_id, OrderEvent.CANCEL, ts=ts)

    def _handle_rejection(self, order_id: OrderId, reason: str, ts: datetime) -> None:
        if self._orders[order_id].state in (
            OrderState.REJECTED, OrderState.CANCELLED, OrderState.FILLED, OrderState.EXPIRED,
        ):
            return
        self._store.append(
            kind=EventKind.ORDER_REJECTED,
            order_id=order_id,
            payload={"reason": reason},
            ts=ts,
        )
        self._orders[order_id] = replace(self._orders[order_id], rejection_reason=reason)
        self._transition(order_id, OrderEvent.REJECT, ts=ts)

    def _handle_expiry(self, order_id: OrderId, ts: datetime) -> None:
        if self._orders[order_id].state in (
            OrderState.EXPIRED, OrderState.CANCELLED, OrderState.FILLED, OrderState.REJECTED,
        ):
            return
        self._store.append(
            kind=EventKind.ORDER_EXPIRED,
            order_id=order_id,
            payload={},
            ts=ts,
        )
        self._transition(order_id, OrderEvent.EXPIRE, ts=ts)

    def _transition(self, order_id: OrderId, event: OrderEvent, ts: datetime) -> None:
        """Drive the FSM and update the Order snapshot's `state` field."""
        fsm = self._fsms[order_id]
        old_state = fsm.state
        ok = fsm.trigger(event)
        if not ok:
            logger.error(
                "Invalid FSM transition for %s: %s + %s (no arc)",
                order_id, old_state, event,
            )
            return
        new_state = fsm.state
        self._orders[order_id] = replace(self._orders[order_id], state=new_state)
        self._bus.publish(
            OrderStateChangedEvent(
                order_id=order_id, old_state=old_state, new_state=new_state, timestamp=ts,
            )
        )

    def _broker_id_to_order_inverse(self) -> dict[OrderId, str]:
        """Inverse of self._broker_id_to_order. Cheap; called rarely."""
        return {oid: bid for bid, oid in self._broker_id_to_order.items()}

    # ─── Recovery internals ───────────────────────────────────────────────────

    def _replay_log(self) -> None:
        """Reconstruct in-memory state from the event log."""
        for evt in self._store.iter_all():
            self._apply_logged_event(evt.kind, evt.order_id, evt.payload, evt.ts)

    def _apply_logged_event(
        self,
        kind: EventKind,
        order_id: OrderId,
        payload: dict[str, Any],
        ts: datetime,
    ) -> None:
        match kind:
            case EventKind.ORDER_SUBMIT_INTENT:
                order = _deserialize_order(payload)
                self._orders[order_id] = order
                self._fsms[order_id] = build_order_fsm(OrderState.PENDING)
                self._fills_by_order.setdefault(order_id, [])
                self._fsms[order_id].trigger(OrderEvent.SUBMIT)
                self._orders[order_id] = replace(self._orders[order_id], state=OrderState.SUBMITTED)
            case EventKind.ORDER_ACCEPTED:
                broker_id = payload["broker_order_id"]
                self._broker_id_to_order[broker_id] = order_id
                self._orders[order_id] = replace(
                    self._orders[order_id], broker_order_id=broker_id, submitted_at=ts,
                )
                self._fsms[order_id].trigger(OrderEvent.ACCEPT)
                self._orders[order_id] = replace(self._orders[order_id], state=OrderState.ACCEPTED)
            case EventKind.ORDER_REJECTED:
                self._orders[order_id] = replace(
                    self._orders[order_id], rejection_reason=payload.get("reason"),
                )
                self._fsms[order_id].trigger(OrderEvent.REJECT)
                self._orders[order_id] = replace(self._orders[order_id], state=OrderState.REJECTED)
            case EventKind.FILL_RECEIVED:
                fill = _deserialize_fill(payload)
                self._fills_by_order.setdefault(order_id, []).append(fill)
                order = self._orders[order_id]
                new_filled = order.filled_qty + fill.qty
                is_full = new_filled >= order.qty
                if order.filled_avg_price is None:
                    new_avg = fill.price
                else:
                    total = order.filled_avg_price * order.filled_qty + fill.price * fill.qty
                    new_avg = total / new_filled
                self._orders[order_id] = replace(
                    order,
                    filled_qty=new_filled,
                    filled_avg_price=new_avg,
                    filled_at=ts if is_full else order.filled_at,
                )
                if is_full:
                    self._fsms[order_id].trigger(OrderEvent.FULL_FILL)
                    self._orders[order_id] = replace(
                        self._orders[order_id], state=OrderState.FILLED,
                    )
                else:
                    self._fsms[order_id].trigger(OrderEvent.PARTIAL_FILL)
                    self._orders[order_id] = replace(
                        self._orders[order_id], state=OrderState.PARTIAL,
                    )
            case EventKind.ORDER_CANCEL_REQUESTED:
                pass  # No state change yet; awaiting broker confirmation
            case EventKind.ORDER_CANCELLED:
                self._fsms[order_id].trigger(OrderEvent.CANCEL)
                self._orders[order_id] = replace(self._orders[order_id], state=OrderState.CANCELLED)
            case EventKind.ORDER_EXPIRED:
                self._fsms[order_id].trigger(OrderEvent.EXPIRE)
                self._orders[order_id] = replace(self._orders[order_id], state=OrderState.EXPIRED)
            case (
                EventKind.RECONCILE_NOOP
                | EventKind.RECONCILE_RECOVERED
                | EventKind.RECONCILE_ABANDONED
            ):
                pass  # Audit-only events

    def _reconcile_open_orders(self) -> ReconcileSummary:
        """For every non-terminal order, ask the broker for the truth."""
        replayed = len(self._orders)
        recovered = 0
        abandoned = 0
        already_terminal = 0

        terminal = {
            OrderState.FILLED, OrderState.CANCELLED,
            OrderState.REJECTED, OrderState.EXPIRED,
        }
        for order_id in list(self._orders.keys()):
            order = self._orders[order_id]
            if order.state in terminal:
                already_terminal += 1
                continue

            broker_status = self._broker.find_order_by_client_id(order_id)
            if broker_status is None:
                # Broker has no record. Two cases:
                #  (a) we crashed before broker.submit_order was called → safe to abandon
                #  (b) broker dropped the order silently → also abandon and alert later
                self._store.append(
                    kind=EventKind.RECONCILE_ABANDONED,
                    order_id=order_id,
                    payload={"reason": "broker has no record after recovery"},
                    ts=self._clock.now(),
                )
                self._handle_rejection(
                    order_id,
                    reason="abandoned_during_recovery",
                    ts=self._clock.now(),
                )
                abandoned += 1
                continue

            if self._broker_state_matches(order, broker_status):
                self._store.append(
                    kind=EventKind.RECONCILE_NOOP,
                    order_id=order_id,
                    payload={"broker_state": str(broker_status.state)},
                    ts=self._clock.now(),
                )
                continue

            # Broker has progress we missed; backfill.
            self._catch_up_from_broker(order_id, broker_status)
            recovered += 1

        return ReconcileSummary(
            orders_replayed=replayed,
            orders_recovered=recovered,
            orders_abandoned=abandoned,
            orders_already_terminal=already_terminal,
        )

    def _broker_state_matches(self, order: Order, broker: BrokerOrderStatus) -> bool:
        """True iff our local view agrees with the broker's view."""
        if order.broker_order_id != broker.broker_order_id:
            return False
        if order.filled_qty != broker.filled_qty:
            return False
        return _broker_state_to_local(broker.state) == order.state

    def _catch_up_from_broker(self, order_id: OrderId, broker: BrokerOrderStatus) -> None:
        """Apply broker-side progress that our log is missing."""
        local = self._orders[order_id]
        ts = self._clock.now()

        if local.broker_order_id is None:
            self._record_accepted(order_id, broker.broker_order_id, ts=ts)
            local = self._orders[order_id]

        # Synthetic fill for the qty we're missing
        delta_qty = broker.filled_qty - local.filled_qty
        if delta_qty > Decimal("0"):
            synthetic_fill = Fill(
                id=new_id(),
                order_id=order_id,
                agent_id=local.agent_id,
                symbol=local.symbol,
                side=local.side,
                qty=delta_qty,
                price=broker.avg_fill_price or Decimal("0"),
                timestamp=ts,
                commission=Decimal("0"),
                is_partial=broker.state != BrokerOrderState.FILLED,
            )
            self._handle_fill(order_id, synthetic_fill, ts=ts)

        # Terminal-state catch-ups
        match broker.state:
            case BrokerOrderState.CANCELED:
                self._handle_cancellation(order_id, ts=ts)
            case BrokerOrderState.REJECTED:
                self._handle_rejection(
                    order_id,
                    reason=broker.rejection_reason or "broker rejected (recovered)",
                    ts=ts,
                )
            case BrokerOrderState.EXPIRED:
                self._handle_expiry(order_id, ts=ts)
            case _:
                pass  # FILLED is handled by the fill above

        self._store.append(
            kind=EventKind.RECONCILE_RECOVERED,
            order_id=order_id,
            payload={"broker_state": str(broker.state), "delta_qty": str(delta_qty)},
            ts=ts,
        )


# ─── Serialization helpers ────────────────────────────────────────────────────


def _serialize_order(order: Order) -> dict[str, Any]:
    """Serialize an Order to a JSON-safe dict (decoder reverses)."""
    return {
        "id": order.id,
        "intent_id": order.intent_id,
        "agent_id": str(order.agent_id),
        "symbol": order.symbol,
        "side": str(order.side),
        "qty": order.qty,
        "order_type": str(order.order_type),
        "order_class": str(order.order_class),
        "time_in_force": str(order.time_in_force),
        "state": str(order.state),
        "created_at": order.created_at,
        "limit_price": order.limit_price,
        "stop_price": order.stop_price,
        "is_letf": order.is_letf,
    }


def _deserialize_order(payload: dict[str, Any]) -> Order:
    from core.types import (  # noqa: PLC0415
        OrderClass,
        OrderSide,
        OrderType,
        TimeInForce,
    )
    return Order(
        id=payload["id"],
        intent_id=payload["intent_id"],
        agent_id=AgentId(payload["agent_id"]),
        symbol=payload["symbol"],
        side=OrderSide(payload["side"]),
        qty=payload["qty"],
        order_type=OrderType(payload["order_type"]),
        order_class=OrderClass(payload["order_class"]),
        time_in_force=TimeInForce(payload["time_in_force"]),
        state=OrderState(payload["state"]),
        created_at=payload["created_at"],
        limit_price=payload.get("limit_price"),
        stop_price=payload.get("stop_price"),
        is_letf=payload.get("is_letf", False),
    )


def _serialize_fill(fill: Fill) -> dict[str, Any]:
    return {
        "id": fill.id,
        "order_id": fill.order_id,
        "agent_id": str(fill.agent_id),
        "symbol": fill.symbol,
        "side": str(fill.side),
        "qty": fill.qty,
        "price": fill.price,
        "timestamp": fill.timestamp,
        "commission": fill.commission,
        "is_partial": fill.is_partial,
    }


def _deserialize_fill(payload: dict[str, Any]) -> Fill:
    from core.types import OrderSide  # noqa: PLC0415
    return Fill(
        id=payload["id"],
        order_id=payload["order_id"],
        agent_id=AgentId(payload["agent_id"]),
        symbol=payload["symbol"],
        side=OrderSide(payload["side"]),
        qty=payload["qty"],
        price=payload["price"],
        timestamp=payload["timestamp"],
        commission=payload.get("commission", Decimal("0")),
        is_partial=payload.get("is_partial", False),
    )


def _broker_state_to_local(broker_state: BrokerOrderState) -> OrderState:
    """Map broker-side state vocabulary to our internal OrderState."""
    match broker_state:
        case BrokerOrderState.NEW | BrokerOrderState.ACCEPTED:
            return OrderState.ACCEPTED
        case BrokerOrderState.PARTIALLY_FILLED:
            return OrderState.PARTIAL
        case BrokerOrderState.FILLED:
            return OrderState.FILLED
        case BrokerOrderState.CANCELED:
            return OrderState.CANCELLED
        case BrokerOrderState.REJECTED:
            return OrderState.REJECTED
        case BrokerOrderState.EXPIRED:
            return OrderState.EXPIRED
        case BrokerOrderState.UNKNOWN:
            return OrderState.PENDING  # caller should treat as needs-reconcile
