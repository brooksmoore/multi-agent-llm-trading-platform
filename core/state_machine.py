"""Generic finite-state machine helper.

Used by the OMS for the Order lifecycle and by kill-switch management.
The FSM is not thread-safe by itself; callers must serialize transitions
(the OMS wraps it in a lock).

Design notes:
- StateT and EventT are generic type params (usually Enums).
- Guards are zero-arg callables returning bool — evaluated at trigger time.
- Actions are zero-arg callables returning None — called after guard passes,
  before the state is updated.  This order ensures the action sees the
  pre-transition state if it needs it.
- Invalid transitions (no registered arc for the current state + event pair)
  return False and leave state unchanged — they do NOT raise.
- Duplicate arcs (same from_state + event) raise ValueError at registration
  time to catch configuration bugs early.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, TypeVar

if TYPE_CHECKING:
    from core.types import OrderEvent, OrderState

StateT = TypeVar("StateT")
EventT = TypeVar("EventT")

Guard = Callable[[], bool]
Action = Callable[[], None]


@dataclass(frozen=True)
class Transition(Generic[StateT, EventT]):
    """One arc in the FSM graph."""

    from_state: StateT
    event: EventT
    to_state: StateT
    guard: Guard | None = None
    action: Action | None = None


class StateMachine(Generic[StateT, EventT]):
    """A deterministic, guarded finite-state machine.

    Usage::

        sm = StateMachine(
            initial_state=OrderState.PENDING,
            transitions=[
                Transition(OrderState.PENDING,   OrderEvent.SUBMIT,       OrderState.SUBMITTED),
                Transition(OrderState.SUBMITTED,  OrderEvent.ACCEPT,       OrderState.ACCEPTED),
                Transition(OrderState.ACCEPTED,   OrderEvent.PARTIAL_FILL, OrderState.PARTIAL),
                Transition(OrderState.ACCEPTED,   OrderEvent.FULL_FILL,    OrderState.FILLED),
                Transition(OrderState.PARTIAL,    OrderEvent.FULL_FILL,    OrderState.FILLED),
                Transition(OrderState.ACCEPTED,   OrderEvent.CANCEL,       OrderState.CANCELLED),
                Transition(OrderState.PARTIAL,    OrderEvent.CANCEL,       OrderState.CANCELLED),
                Transition(OrderState.PENDING,    OrderEvent.REJECT,       OrderState.REJECTED),
                Transition(OrderState.SUBMITTED,  OrderEvent.REJECT,       OrderState.REJECTED),
                Transition(OrderState.ACCEPTED,   OrderEvent.REJECT,       OrderState.REJECTED),
                Transition(OrderState.ACCEPTED,   OrderEvent.EXPIRE,       OrderState.EXPIRED),
                Transition(OrderState.PARTIAL,    OrderEvent.EXPIRE,       OrderState.EXPIRED),
            ],
        )
        ok = sm.trigger(OrderEvent.SUBMIT)   # True → state is now SUBMITTED
        ok = sm.trigger(OrderEvent.CANCEL)   # False → no arc from SUBMITTED+CANCEL
    """

    def __init__(
        self,
        initial_state: StateT,
        transitions: list[Transition[StateT, EventT]],
    ) -> None:
        self._state: StateT = initial_state
        self._table: dict[tuple[StateT, EventT], Transition[StateT, EventT]] = {}
        for t in transitions:
            key = (t.from_state, t.event)
            if key in self._table:
                raise ValueError(
                    f"Duplicate transition arc: ({t.from_state!r}, {t.event!r})"
                )
            self._table[key] = t
        self._history: list[tuple[StateT, EventT, StateT]] = []

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def state(self) -> StateT:
        return self._state

    @property
    def history(self) -> list[tuple[StateT, EventT, StateT]]:
        """Immutable view of (from, event, to) triples, oldest first."""
        return list(self._history)

    # ── Core operations ───────────────────────────────────────────────────────

    def can_trigger(self, event: EventT) -> bool:
        """Return True if the event has a valid arc AND the guard passes."""
        key = (self._state, event)
        t = self._table.get(key)
        if t is None:
            return False
        return t.guard is None or t.guard()

    def trigger(self, event: EventT) -> bool:
        """Attempt the transition.

        Returns True and updates state if the arc exists and guard passes.
        Returns False (no side effects) otherwise.
        """
        key = (self._state, event)
        t = self._table.get(key)
        if t is None:
            return False
        if t.guard is not None and not t.guard():
            return False
        # Execute action before updating state (sees pre-transition state)
        if t.action is not None:
            t.action()
        old_state = self._state
        self._state = t.to_state
        self._history.append((old_state, event, t.to_state))
        return True

    def valid_events(self) -> list[EventT]:
        """Return all events that have arcs from the current state."""
        return [event for (state, event) in self._table if state == self._state]

    def reset(self, state: StateT) -> None:
        """Force the FSM to a specific state (used by OMS crash-recovery replay)."""
        self._history.append((self._state, _RESET_SENTINEL, state))  # type: ignore[arg-type]
        self._state = state


# Sentinel used in history for forced resets — not a real event type
_RESET_SENTINEL = object()


# ─── Pre-built Order lifecycle FSM ────────────────────────────────────────────

def build_order_fsm(initial_state: OrderState) -> StateMachine[OrderState, OrderEvent]:
    """Return a fully-wired Order state machine."""
    from core.types import OrderEvent, OrderState  # noqa: PLC0415  # runtime import

    return StateMachine(
        initial_state=initial_state,
        transitions=[
            Transition(OrderState.PENDING,    OrderEvent.SUBMIT,       OrderState.SUBMITTED),
            Transition(OrderState.SUBMITTED,  OrderEvent.ACCEPT,       OrderState.ACCEPTED),
            Transition(OrderState.ACCEPTED,   OrderEvent.PARTIAL_FILL, OrderState.PARTIAL),
            Transition(OrderState.ACCEPTED,   OrderEvent.FULL_FILL,    OrderState.FILLED),
            Transition(OrderState.PARTIAL,    OrderEvent.FULL_FILL,    OrderState.FILLED),
            Transition(OrderState.PARTIAL,    OrderEvent.PARTIAL_FILL, OrderState.PARTIAL),
            Transition(OrderState.SUBMITTED,  OrderEvent.CANCEL,       OrderState.CANCELLED),
            Transition(OrderState.ACCEPTED,   OrderEvent.CANCEL,       OrderState.CANCELLED),
            Transition(OrderState.PARTIAL,    OrderEvent.CANCEL,       OrderState.CANCELLED),
            Transition(OrderState.PENDING,    OrderEvent.REJECT,       OrderState.REJECTED),
            Transition(OrderState.SUBMITTED,  OrderEvent.REJECT,       OrderState.REJECTED),
            Transition(OrderState.ACCEPTED,   OrderEvent.REJECT,       OrderState.REJECTED),
            Transition(OrderState.ACCEPTED,   OrderEvent.EXPIRE,       OrderState.EXPIRED),
            Transition(OrderState.PARTIAL,    OrderEvent.EXPIRE,       OrderState.EXPIRED),
        ],
    )


# ─── KillSwitch FSM ───────────────────────────────────────────────────────────

# A simpler enum-driven FSM for the global kill-switch state is handled
# directly by kill_switch.py; the StateMachine above can be reused for it
# by instantiating with KillSwitchState/events if needed.
