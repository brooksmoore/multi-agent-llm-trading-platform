"""Deterministic in-memory broker for tests and offline development.

The FakeBroker implements every Broker contract guarantee:
- Idempotent submit_order on client_order_id.
- find_order_by_client_id works for crash-recovery tests.
- Async fill events delivered via the registered callback (synchronously
  in this implementation — broker thread = caller thread).

It also exposes test-only knobs that are NOT part of the Broker Protocol:
- set_fill_mode / set_price / force_fill / force_partial_fill / force_reject
- crash injection (raise on next submit / cancel)
- inspection helpers (open_orders, all_fills)

Treat anything outside the Broker Protocol as test-only — the OMS must
never call those methods.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from core.clock import Clock, WallClock
from core.types import (
    AgentId,
    AssetClass,
    Fill,
    Order,
    OrderId,
    OrderSide,
    OrderType,
    new_id,
)
from execution.broker import (
    BrokerAccount,
    BrokerError,
    BrokerEventCallback,
    BrokerOrderEvent,
    BrokerOrderState,
    BrokerOrderStatus,
    BrokerPosition,
    BrokerRejection,
    BrokerUnavailable,
)

# ─── Test-only knobs ──────────────────────────────────────────────────────────


class FillMode(StrEnum):
    """How the FakeBroker should treat newly submitted orders."""

    INSTANT = "instant"          # Auto-fill at current_price as soon as accepted
    MANUAL = "manual"            # Sit in ACCEPTED state until force_*() is called
    REJECT = "reject"            # Reject every submit
    PARTIAL_THEN_HOLD = "partial_then_hold"  # Fill half on accept, hold remainder


@dataclass
class _BrokerOrder:
    """Mutable internal record of a broker-side order.

    Stores the originating agent_id so emitted Fills carry it correctly.
    A real broker (Alpaca) doesn't expose this — the OMS attaches it on
    the way out. For FakeBroker we keep it locally.
    """

    status: BrokerOrderStatus
    fills: list[Fill]
    agent_id: AgentId
    limit_price: Decimal | None = None


# ─── FakeBroker ───────────────────────────────────────────────────────────────


class FakeBroker:
    """In-memory broker for tests + offline dev.

    Implements the `Broker` Protocol structurally; not declared as inheriting
    so we keep duck-typing intent clear.
    """

    # ---- Construction ----

    def __init__(
        self,
        clock: Clock | None = None,
        fill_mode: FillMode = FillMode.INSTANT,
        starting_cash: Decimal = Decimal("30000"),
    ) -> None:
        self._clock: Clock = clock if clock is not None else WallClock()
        self._fill_mode: FillMode = fill_mode
        self._starting_cash: Decimal = starting_cash
        self._cash: Decimal = starting_cash

        # broker_order_id -> internal record
        self._orders: dict[str, _BrokerOrder] = {}
        # client_order_id (our UUID) -> broker_order_id (idempotency map)
        self._client_to_broker: dict[OrderId, str] = {}
        # symbol -> aggregated position
        self._positions: dict[str, BrokerPosition] = {}
        # Per-symbol price feed (for fills); fallback to limit_price or 1.0
        self._prices: dict[str, Decimal] = {}

        self._callback: BrokerEventCallback | None = None
        self._lock = threading.RLock()

        # Crash-injection knobs
        self._raise_on_next_submit: BrokerError | None = None
        self._raise_on_next_cancel: BrokerError | None = None

    # ---- Test-only configuration ----

    def set_fill_mode(self, mode: FillMode) -> None:
        with self._lock:
            self._fill_mode = mode

    def set_price(self, symbol: str, price: Decimal) -> None:
        with self._lock:
            self._prices[symbol] = price

    def inject_submit_failure(self, error: BrokerError) -> None:
        """Make the next submit_order() call raise this error. Test-only."""
        self._raise_on_next_submit = error

    def inject_cancel_failure(self, error: BrokerError) -> None:
        self._raise_on_next_cancel = error

    def force_full_fill(self, client_order_id: OrderId, price: Decimal | None = None) -> None:
        """Manually fill an order completely. Used in MANUAL mode."""
        with self._lock:
            broker_id = self._client_to_broker.get(client_order_id)
            if broker_id is None:
                raise KeyError(f"No order with client_id={client_order_id}")
            rec = self._orders[broker_id]
            remaining = rec.status.qty - rec.status.filled_qty
            if remaining <= Decimal("0"):
                return
            fill_price = price if price is not None else self._price_for(rec.status.symbol)
            self._record_fill(rec, remaining, fill_price)

    def force_partial_fill(
        self,
        client_order_id: OrderId,
        qty: Decimal,
        price: Decimal | None = None,
    ) -> None:
        with self._lock:
            broker_id = self._client_to_broker.get(client_order_id)
            if broker_id is None:
                raise KeyError(f"No order with client_id={client_order_id}")
            rec = self._orders[broker_id]
            remaining = rec.status.qty - rec.status.filled_qty
            if qty > remaining:
                raise ValueError(f"Partial fill qty {qty} exceeds remaining {remaining}")
            fill_price = price if price is not None else self._price_for(rec.status.symbol)
            self._record_fill(rec, qty, fill_price)

    def force_reject(self, client_order_id: OrderId, reason: str) -> None:
        with self._lock:
            broker_id = self._client_to_broker.get(client_order_id)
            if broker_id is None:
                raise KeyError(f"No order with client_id={client_order_id}")
            rec = self._orders[broker_id]
            now = self._clock.now()
            rec.status = replace(
                rec.status,
                state=BrokerOrderState.REJECTED,
                rejection_reason=reason,
                updated_at=now,
            )
            self._emit(
                BrokerOrderEvent(
                    broker_order_id=broker_id,
                    client_order_id=client_order_id,
                    new_state=BrokerOrderState.REJECTED,
                    rejection_reason=reason,
                    timestamp=now,
                )
            )

    def open_orders(self) -> list[BrokerOrderStatus]:
        with self._lock:
            return [
                rec.status for rec in self._orders.values()
                if rec.status.state in (
                    BrokerOrderState.NEW,
                    BrokerOrderState.ACCEPTED,
                    BrokerOrderState.PARTIALLY_FILLED,
                )
            ]

    def all_fills(self) -> list[Fill]:
        with self._lock:
            out: list[Fill] = []
            for rec in self._orders.values():
                out.extend(rec.fills)
            return out

    # ---- Broker Protocol implementation ----

    def submit_order(self, order: Order) -> str:
        with self._lock:
            if self._raise_on_next_submit is not None:
                err = self._raise_on_next_submit
                self._raise_on_next_submit = None
                raise err

            # Idempotency check
            existing = self._client_to_broker.get(order.id)
            if existing is not None:
                return existing

            # Reject mode
            if self._fill_mode == FillMode.REJECT:
                broker_id = str(uuid.uuid4())
                self._client_to_broker[order.id] = broker_id
                now = self._clock.now()
                status = BrokerOrderStatus(
                    broker_order_id=broker_id,
                    client_order_id=order.id,
                    symbol=order.symbol,
                    side=order.side,
                    qty=order.qty,
                    filled_qty=Decimal("0"),
                    avg_fill_price=None,
                    state=BrokerOrderState.REJECTED,
                    submitted_at=now,
                    updated_at=now,
                    rejection_reason="fake_broker REJECT mode",
                )
                self._orders[broker_id] = _BrokerOrder(
                    status=status, fills=[], agent_id=order.agent_id, limit_price=order.limit_price,
                )
                # Fire rejected event AFTER we've recorded internal state
                self._emit(
                    BrokerOrderEvent(
                        broker_order_id=broker_id,
                        client_order_id=order.id,
                        new_state=BrokerOrderState.REJECTED,
                        rejection_reason="fake_broker REJECT mode",
                        timestamp=now,
                    )
                )
                raise BrokerRejection("fake_broker REJECT mode")

            # Normal acceptance path
            broker_id = str(uuid.uuid4())
            self._client_to_broker[order.id] = broker_id
            now = self._clock.now()
            status = BrokerOrderStatus(
                broker_order_id=broker_id,
                client_order_id=order.id,
                symbol=order.symbol,
                side=order.side,
                qty=order.qty,
                filled_qty=Decimal("0"),
                avg_fill_price=None,
                state=BrokerOrderState.ACCEPTED,
                submitted_at=now,
                updated_at=now,
            )
            rec = _BrokerOrder(
                status=status, fills=[], agent_id=order.agent_id, limit_price=order.limit_price,
            )
            self._orders[broker_id] = rec

            # Fire ACCEPTED event so the OMS knows the broker assigned an ID
            self._emit(
                BrokerOrderEvent(
                    broker_order_id=broker_id,
                    client_order_id=order.id,
                    new_state=BrokerOrderState.ACCEPTED,
                    timestamp=now,
                )
            )

            # Optional auto-fill behavior
            if self._fill_mode == FillMode.INSTANT:
                fill_price = self._price_for(order.symbol, order.limit_price)
                self._record_fill(rec, order.qty, fill_price)
            elif self._fill_mode == FillMode.PARTIAL_THEN_HOLD:
                half = order.qty / Decimal("2")
                fill_price = self._price_for(order.symbol, order.limit_price)
                self._record_fill(rec, half, fill_price)

            return broker_id

    def cancel_order(self, broker_order_id: str) -> None:
        with self._lock:
            if self._raise_on_next_cancel is not None:
                err = self._raise_on_next_cancel
                self._raise_on_next_cancel = None
                raise err
            rec = self._orders.get(broker_order_id)
            if rec is None:
                raise BrokerUnavailable(f"Unknown broker_order_id={broker_order_id}")
            if rec.status.state in (
                BrokerOrderState.FILLED,
                BrokerOrderState.CANCELED,
                BrokerOrderState.REJECTED,
                BrokerOrderState.EXPIRED,
            ):
                # No-op: already terminal
                return
            now = self._clock.now()
            rec.status = replace(rec.status, state=BrokerOrderState.CANCELED, updated_at=now)
            self._emit(
                BrokerOrderEvent(
                    broker_order_id=broker_order_id,
                    client_order_id=rec.status.client_order_id,
                    new_state=BrokerOrderState.CANCELED,
                    timestamp=now,
                )
            )

    def get_order(self, broker_order_id: str) -> BrokerOrderStatus:
        with self._lock:
            rec = self._orders.get(broker_order_id)
            if rec is None:
                raise BrokerUnavailable(f"Unknown broker_order_id={broker_order_id}")
            return rec.status

    def find_order_by_client_id(self, client_order_id: OrderId) -> BrokerOrderStatus | None:
        with self._lock:
            broker_id = self._client_to_broker.get(client_order_id)
            if broker_id is None:
                return None
            return self._orders[broker_id].status

    def list_positions(self) -> list[BrokerPosition]:
        with self._lock:
            return list(self._positions.values())

    def get_account(self) -> BrokerAccount:
        with self._lock:
            equity = self._cash + sum(
                (p.qty * p.current_price for p in self._positions.values()),
                start=Decimal("0"),
            )
            return BrokerAccount(
                cash=self._cash,
                equity=equity,
                buying_power=self._cash * Decimal("2"),  # naive Reg-T 2x
                pattern_day_trader=False,
                daytrade_count=0,
            )

    def register_event_callback(self, callback: BrokerEventCallback) -> None:
        with self._lock:
            self._callback = callback

    # ---- Internal helpers ----

    def _price_for(self, symbol: str, fallback: Decimal | None = None) -> Decimal:
        if symbol in self._prices:
            return self._prices[symbol]
        if fallback is not None:
            return fallback
        return Decimal("100.00")  # arbitrary default for tests

    def _record_fill(
        self,
        rec: _BrokerOrder,
        qty: Decimal,
        price: Decimal,
    ) -> None:
        """Record a fill against `rec`. Caller MUST hold self._lock."""
        now = self._clock.now()
        new_filled = rec.status.filled_qty + qty
        is_full = new_filled >= rec.status.qty

        # New average fill price (weighted)
        if rec.status.avg_fill_price is None:
            new_avg = price
        else:
            total_value = (rec.status.avg_fill_price * rec.status.filled_qty) + (price * qty)
            new_avg = total_value / new_filled

        new_state = BrokerOrderState.FILLED if is_full else BrokerOrderState.PARTIALLY_FILLED
        rec.status = replace(
            rec.status,
            filled_qty=new_filled,
            avg_fill_price=new_avg,
            state=new_state,
            updated_at=now,
        )

        fill = Fill(
            id=new_id(),
            order_id=rec.status.client_order_id,
            agent_id=rec.agent_id,
            symbol=rec.status.symbol,
            side=rec.status.side,
            qty=qty,
            price=price,
            timestamp=now,
            commission=Decimal("0"),
            is_partial=not is_full,
        )
        rec.fills.append(fill)

        # Update positions/cash
        self._apply_fill_to_account(rec.status.symbol, rec.status.side, qty, price)

        self._emit(
            BrokerOrderEvent(
                broker_order_id=rec.status.broker_order_id,
                client_order_id=rec.status.client_order_id,
                new_state=new_state,
                fill=fill,
                timestamp=now,
            )
        )

    def _apply_fill_to_account(
        self,
        symbol: str,
        side: OrderSide,
        qty: Decimal,
        price: Decimal,
    ) -> None:
        signed_qty = qty if side == OrderSide.BUY else -qty
        self._cash -= signed_qty * price

        existing = self._positions.get(symbol)
        if existing is None:
            if signed_qty == Decimal("0"):
                return
            self._positions[symbol] = BrokerPosition(
                symbol=symbol,
                qty=signed_qty,
                avg_entry_price=price,
                current_price=price,
                asset_class=AssetClass.EQUITY,  # FakeBroker doesn't distinguish
            )
            return

        new_qty = existing.qty + signed_qty
        if new_qty == Decimal("0"):
            del self._positions[symbol]
            return
        # Weighted average only when adding to position; reduce keeps avg_entry_price
        if (existing.qty > 0 and signed_qty > 0) or (existing.qty < 0 and signed_qty < 0):
            total_value = existing.avg_entry_price * existing.qty + price * signed_qty
            new_avg = total_value / new_qty
        else:
            new_avg = existing.avg_entry_price
        self._positions[symbol] = replace(
            existing, qty=new_qty, avg_entry_price=new_avg, current_price=price,
        )

    def _emit(self, event: BrokerOrderEvent) -> None:
        """Fire the registered callback. Caller may hold self._lock; callback runs in-thread."""
        cb = self._callback
        if cb is not None:
            cb(event)


# ─── Test helpers ─────────────────────────────────────────────────────────────


def make_market_order(
    *,
    symbol: str,
    side: OrderSide,
    qty: Decimal,
    agent_id: AgentId,
    clock: Clock | None = None,
) -> Order:
    """Convenience constructor for tests (lives here so test files stay short)."""
    from core.types import (  # noqa: PLC0415
        OrderClass,
        OrderState,
        TimeInForce,
    )
    c = clock if clock is not None else WallClock()
    return Order(
        id=new_id(),
        intent_id=new_id(),
        agent_id=agent_id,
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=OrderType.MARKET,
        order_class=OrderClass.SIMPLE,
        time_in_force=TimeInForce.DAY,
        state=OrderState.PENDING,
        created_at=c.now(),
    )
