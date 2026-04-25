"""Broker abstraction (Protocol) + broker-side data types.

Hexagonal principle: the OMS depends only on this module, not on alpaca-py.
Concrete adapters live in fake_broker.py (M2) and alpaca_broker.py (M4).

The contract is intentionally minimal — just enough for the OMS to:
- Submit an order idempotently (via client_order_id)
- Cancel an open order
- Query an order's current status
- List open positions and account state
- Register an async callback for order/fill events

A broker MUST be idempotent on `submit_order(client_order_id=X)` — submitting
twice with the same client_order_id returns the same broker_order_id without
creating a duplicate. This is the contract that makes crash-recovery safe.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Protocol, runtime_checkable

from core.types import (
    AssetClass,
    Fill,
    Order,
    OrderId,
    OrderSide,
)

# ─── Broker-side data types ───────────────────────────────────────────────────


class BrokerOrderState(StrEnum):
    """The broker's view of an order's lifecycle.

    Distinct from our internal OrderState because brokers expose a different
    vocabulary (e.g. Alpaca uses 'new', 'partially_filled', 'done_for_day').
    Adapters translate to/from this canonical broker view.
    """

    NEW = "new"                       # Accepted by broker, not yet routed
    ACCEPTED = "accepted"             # Routed to venue
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    UNKNOWN = "unknown"               # Broker has no record


@dataclass(frozen=True)
class BrokerOrderStatus:
    """Snapshot of a broker-side order. Returned by Broker.get_order()."""

    broker_order_id: str
    client_order_id: OrderId          # our UUID, the idempotency key
    symbol: str
    side: OrderSide
    qty: Decimal
    filled_qty: Decimal
    avg_fill_price: Decimal | None
    state: BrokerOrderState
    submitted_at: datetime
    updated_at: datetime
    rejection_reason: str | None = None


@dataclass(frozen=True)
class BrokerPosition:
    """Broker's view of a held position."""

    symbol: str
    qty: Decimal                  # signed: positive long, negative short
    avg_entry_price: Decimal
    current_price: Decimal
    asset_class: AssetClass


@dataclass(frozen=True)
class BrokerAccount:
    """Account-level snapshot from the broker."""

    cash: Decimal
    equity: Decimal               # cash + market value of positions
    buying_power: Decimal         # may be > equity due to margin
    pattern_day_trader: bool
    daytrade_count: int


# ─── Broker callback / event types ────────────────────────────────────────────


@dataclass(frozen=True)
class BrokerOrderEvent:
    """An asynchronous event from the broker about one of our orders.

    Broker adapters wrap their websocket / poll loop and emit these events
    to a single registered callback.
    """

    broker_order_id: str
    client_order_id: OrderId
    new_state: BrokerOrderState
    fill: Fill | None = None        # populated on FILL events
    rejection_reason: str | None = None
    timestamp: datetime | None = None


# Callback signature: broker emits these; OMS handles them.
BrokerEventCallback = Callable[[BrokerOrderEvent], None]


# ─── Broker exceptions ────────────────────────────────────────────────────────


class BrokerError(Exception):
    """Base for all broker-related failures."""


class BrokerRejection(BrokerError):
    """Broker actively rejected an order (vs. transient network failure)."""


class BrokerUnavailable(BrokerError):
    """Broker is unreachable or returned 5xx; safe to retry with same client_order_id."""


# ─── Broker Protocol ──────────────────────────────────────────────────────────


@runtime_checkable
class Broker(Protocol):
    """Minimal broker interface required by the OMS.

    Implementations:
        - FakeBroker (M2): in-memory, deterministic, configurable fill behavior
        - AlpacaBroker (M4): wraps alpaca-py TradingClient + TradingStream
    """

    def submit_order(self, order: Order) -> str:
        """Submit an order. Returns broker_order_id.

        MUST be idempotent on `order.id` (the client_order_id):
        - First call: creates a new broker order, returns a fresh broker_order_id.
        - Subsequent calls with the same `order.id`: return the SAME broker_order_id
          without creating a duplicate.

        Raises:
            BrokerRejection — broker actively rejected the order.
            BrokerUnavailable — transient failure; safe to retry.
        """
        ...

    def cancel_order(self, broker_order_id: str) -> None:
        """Request cancellation. May complete asynchronously via callback."""
        ...

    def get_order(self, broker_order_id: str) -> BrokerOrderStatus:
        """Fetch the current broker-side status of an order."""
        ...

    def find_order_by_client_id(self, client_order_id: OrderId) -> BrokerOrderStatus | None:
        """Look up an order by our idempotency key. Returns None if broker has no record.

        Used during crash recovery: if we logged ORDER_SUBMIT_INTENT but
        no ORDER_ACCEPTED, this tells us whether the broker received the order.
        """
        ...

    def list_positions(self) -> list[BrokerPosition]:
        """All open positions across the account."""
        ...

    def get_account(self) -> BrokerAccount:
        """Current account snapshot."""
        ...

    def register_event_callback(self, callback: BrokerEventCallback) -> None:
        """Subscribe to async order/fill events from the broker.

        The callback fires for every state change on every order owned by
        this broker connection. The OMS uses this to react to fills and
        cancellations.

        Calling this twice replaces the previous callback.
        """
        ...
