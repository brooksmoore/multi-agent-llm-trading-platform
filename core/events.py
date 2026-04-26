"""In-process event bus and typed event dataclasses.

The EventBus is a simple synchronous observer: publish() calls all registered
handlers in-thread, in registration order. For the main loop's use case
(single-process, no async boundaries at the bus layer) this is sufficient.

Thread safety: subscribe/unsubscribe/publish are all protected by a single
lock. Handlers must not call subscribe/unsubscribe re-entrantly.
"""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from core.types import (
    AgentId,
    Fill,
    Intent,
    IntentId,
    KillSwitchState,
    Order,
    OrderId,
    OrderState,
)

# ─── Event base + typed events ────────────────────────────────────────────────


@dataclass(frozen=True)
class Event:
    """Base class for all events on the bus."""

    name: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class IntentSubmittedEvent(Event):
    name: str = field(default="intent.submitted", init=False)
    intent: Intent = field(default_factory=lambda: _missing_intent())


@dataclass(frozen=True)
class IntentApprovedEvent(Event):
    name: str = field(default="intent.approved", init=False)
    intent_id: IntentId = field(default_factory=uuid.uuid4)


@dataclass(frozen=True)
class IntentRejectedEvent(Event):
    name: str = field(default="intent.rejected", init=False)
    intent_id: IntentId = field(default_factory=uuid.uuid4)
    reason: str = ""


@dataclass(frozen=True)
class OrderPlacedEvent(Event):
    name: str = field(default="order.placed", init=False)
    order: Order = field(default_factory=lambda: _missing_order())


@dataclass(frozen=True)
class OrderStateChangedEvent(Event):
    name: str = field(default="order.state_changed", init=False)
    order_id: OrderId = field(default_factory=uuid.uuid4)
    old_state: OrderState = OrderState.PENDING
    new_state: OrderState = OrderState.PENDING


@dataclass(frozen=True)
class FillReceivedEvent(Event):
    name: str = field(default="fill.received", init=False)
    fill: Fill = field(default_factory=lambda: _missing_fill())


@dataclass(frozen=True)
class KillSwitchTrippedEvent(Event):
    name: str = field(default="kill_switch.tripped", init=False)
    agent_id: AgentId | None = None
    new_state: KillSwitchState = KillSwitchState.OK
    reason: str = ""


@dataclass(frozen=True)
class KillSwitchResetEvent(Event):
    name: str = field(default="kill_switch.reset", init=False)
    agent_id: AgentId | None = None


@dataclass(frozen=True)
class ReconciliationBreakEvent(Event):
    name: str = field(default="reconciliation.break", init=False)
    symbol: str = ""
    local_qty: Decimal = Decimal("0")
    broker_qty: Decimal = Decimal("0")
    delta_usd: Decimal = Decimal("0")


@dataclass(frozen=True)
class HeartbeatMissedEvent(Event):
    name: str = field(default="heartbeat.missed", init=False)
    last_tick_age_seconds: float = 0.0


@dataclass(frozen=True)
class BudgetExhaustedEvent(Event):
    name: str = field(default="budget.exhausted", init=False)
    agent_id: AgentId | None = None
    spent_today: Decimal = Decimal("0")


@dataclass(frozen=True)
class AgentBenchedEvent(Event):
    name: str = field(default="agent.benched", init=False)
    agent_id: AgentId = AgentId.HAIKU
    consecutive_losses: int = 0


@dataclass(frozen=True)
class DrawdownLadderFiredEvent(Event):
    name: str = field(default="drawdown.ladder_fired", init=False)
    agent_id: AgentId | None = None
    drawdown_pct: Decimal = Decimal("0")
    new_bucket: str = ""


@dataclass(frozen=True)
class LETFAutoLiquidatedEvent(Event):
    name: str = field(default="letf.auto_liquidated", init=False)
    agent_id: AgentId = AgentId.HAIKU
    symbol: str = ""
    held_days: int = 0


@dataclass(frozen=True)
class LeverageRotationFlagEvent(Event):
    """Emitted when an agent reopens equivalent LETF exposure ≥3 times in the window."""

    name: str = field(default="leverage.rotation_flag", init=False)
    agent_id: AgentId = AgentId.HAIKU
    symbol: str = ""
    category: str = ""
    reopen_count: int = 0


@dataclass(frozen=True)
class IntentSizedEvent(Event):
    """Emitted by ExecutionPlanner after sizing an intent into an order."""

    name: str = field(default="intent.sized", init=False)
    intent_id: IntentId = field(default_factory=uuid.uuid4)
    agent_id: AgentId = AgentId.HAIKU
    symbol: str = ""
    target_weight: Decimal = Decimal("0")
    position_value_usd: Decimal = Decimal("0")
    qty: Decimal = Decimal("0")
    effective_vol_target: Decimal = Decimal("0")
    effective_max_gross_val: Decimal = Decimal("0")
    realized_vol_30d: Decimal = Decimal("0")
    binding_constraint: str = ""  # "vol_target" | "max_gross" | "close"


# ─── Sentinel factories (only used as dataclass defaults) ─────────────────────

def _missing_intent() -> Intent:
    raise RuntimeError("IntentSubmittedEvent created without an intent")


def _missing_order() -> Order:
    raise RuntimeError("OrderPlacedEvent created without an order")


def _missing_fill() -> Fill:
    raise RuntimeError("FillReceivedEvent created without a fill")


# ─── EventBus ─────────────────────────────────────────────────────────────────

EventHandler = Callable[[Event], None]


class EventBus:
    """Synchronous, thread-safe in-process publish/subscribe bus."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, event_name: str, handler: EventHandler) -> None:
        with self._lock:
            self._handlers[event_name].append(handler)

    def unsubscribe(self, event_name: str, handler: EventHandler) -> None:
        with self._lock:
            self._handlers[event_name] = [
                h for h in self._handlers[event_name] if h is not handler
            ]

    def publish(self, event: Event) -> None:
        """Dispatch event to name-specific handlers AND wildcard subscribers."""
        with self._lock:
            specific = list(self._handlers.get(event.name, []))
            wildcard = list(self._handlers.get("*", []))
        for handler in specific + wildcard:
            handler(event)

    def subscribe_all(self, handler: EventHandler) -> None:
        """Register a handler that receives every event regardless of name."""
        self.subscribe("*", handler)

    # Alias retained for legacy M1 test code; identical to publish().
    publish_all = publish
