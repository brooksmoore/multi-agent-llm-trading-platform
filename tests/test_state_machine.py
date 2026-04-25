"""Tests for core/state_machine.py.

Covers:
- Valid and invalid transitions on a simple 3-state FSM
- Guard evaluation (pass and fail)
- Action callbacks (called on valid transition, not on failed guard)
- Duplicate arc registration raises ValueError
- history tracking
- reset() for crash-recovery
- The pre-built Order lifecycle FSM (build_order_fsm)
- Clock: WallClock and BacktestClock behaviour
- EventBus: subscribe/publish/unsubscribe, wildcard handlers
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum, auto

import pytest

from core.clock import BacktestClock
from core.events import (
    EventBus,
    IntentRejectedEvent,
    KillSwitchTrippedEvent,
    OrderStateChangedEvent,
)
from core.state_machine import StateMachine, Transition, build_order_fsm
from core.types import (
    KillSwitchState,
    OrderEvent,
    OrderState,
)

# ─── Minimal test FSM (Traffic light) ─────────────────────────────────────────


class Light(Enum):
    RED = auto()
    YELLOW = auto()
    GREEN = auto()


class Signal(Enum):
    ADVANCE = auto()
    RESET = auto()


def _traffic_light_fsm(initial: Light = Light.RED) -> StateMachine[Light, Signal]:
    return StateMachine(
        initial_state=initial,
        transitions=[
            Transition(Light.RED,    Signal.ADVANCE, Light.GREEN),
            Transition(Light.GREEN,  Signal.ADVANCE, Light.YELLOW),
            Transition(Light.YELLOW, Signal.ADVANCE, Light.RED),
            # RESET from any state → RED
            Transition(Light.RED,    Signal.RESET,   Light.RED),
            Transition(Light.GREEN,  Signal.RESET,   Light.RED),
            Transition(Light.YELLOW, Signal.RESET,   Light.RED),
        ],
    )


class TestStateMachineBasics:

    def test_initial_state(self) -> None:
        sm = _traffic_light_fsm()
        assert sm.state == Light.RED

    def test_valid_transition_returns_true(self) -> None:
        sm = _traffic_light_fsm()
        result = sm.trigger(Signal.ADVANCE)
        assert result is True
        assert sm.state == Light.GREEN

    def test_full_cycle(self) -> None:
        sm = _traffic_light_fsm()
        sm.trigger(Signal.ADVANCE)   # RED → GREEN
        sm.trigger(Signal.ADVANCE)   # GREEN → YELLOW
        sm.trigger(Signal.ADVANCE)   # YELLOW → RED
        assert sm.state == Light.RED

    def test_invalid_transition_returns_false(self) -> None:
        # Create a minimal FSM that is missing a RESET arc — triggering RESET should fail.
        sm2: StateMachine[Light, Signal] = StateMachine(
            initial_state=Light.RED,
            transitions=[
                Transition(Light.RED, Signal.ADVANCE, Light.GREEN),
                # No RESET arc registered — triggering RESET should fail
            ],
        )
        result = sm2.trigger(Signal.RESET)
        assert result is False
        assert sm2.state == Light.RED  # unchanged

    def test_reset_from_any_state(self) -> None:
        sm = _traffic_light_fsm()
        sm.trigger(Signal.ADVANCE)  # → GREEN
        sm.trigger(Signal.RESET)    # → RED
        assert sm.state == Light.RED

    def test_history_recorded(self) -> None:
        sm = _traffic_light_fsm()
        sm.trigger(Signal.ADVANCE)
        sm.trigger(Signal.ADVANCE)
        h = sm.history
        assert len(h) == 2
        assert h[0] == (Light.RED, Signal.ADVANCE, Light.GREEN)
        assert h[1] == (Light.GREEN, Signal.ADVANCE, Light.YELLOW)

    def test_failed_transition_not_in_history(self) -> None:
        sm2: StateMachine[Light, Signal] = StateMachine(
            initial_state=Light.RED,
            transitions=[Transition(Light.RED, Signal.ADVANCE, Light.GREEN)],
        )
        sm2.trigger(Signal.RESET)  # no arc — returns False
        assert sm2.history == []

    def test_duplicate_arc_raises(self) -> None:
        with pytest.raises(ValueError, match="Duplicate transition arc"):
            StateMachine(
                initial_state=Light.RED,
                transitions=[
                    Transition(Light.RED, Signal.ADVANCE, Light.GREEN),
                    Transition(Light.RED, Signal.ADVANCE, Light.YELLOW),  # duplicate!
                ],
            )

    def test_valid_events(self) -> None:
        sm = _traffic_light_fsm()
        events = sm.valid_events()
        assert Signal.ADVANCE in events
        assert Signal.RESET in events

    def test_force_reset(self) -> None:
        sm = _traffic_light_fsm()
        sm.trigger(Signal.ADVANCE)   # → GREEN
        sm.reset(Light.RED)           # crash-recovery reset
        assert sm.state == Light.RED


class TestGuards:

    def test_guard_pass_allows_transition(self) -> None:
        allowed = True
        sm: StateMachine[Light, Signal] = StateMachine(
            initial_state=Light.RED,
            transitions=[
                Transition(Light.RED, Signal.ADVANCE, Light.GREEN, guard=lambda: allowed),
            ],
        )
        result = sm.trigger(Signal.ADVANCE)
        assert result is True
        assert sm.state == Light.GREEN

    def test_guard_fail_blocks_transition(self) -> None:
        blocked = False
        sm: StateMachine[Light, Signal] = StateMachine(
            initial_state=Light.RED,
            transitions=[
                Transition(Light.RED, Signal.ADVANCE, Light.GREEN, guard=lambda: blocked),
            ],
        )
        result = sm.trigger(Signal.ADVANCE)
        assert result is False
        assert sm.state == Light.RED   # unchanged

    def test_guard_fail_not_recorded_in_history(self) -> None:
        sm: StateMachine[Light, Signal] = StateMachine(
            initial_state=Light.RED,
            transitions=[
                Transition(Light.RED, Signal.ADVANCE, Light.GREEN, guard=lambda: False),
            ],
        )
        sm.trigger(Signal.ADVANCE)
        assert sm.history == []

    def test_can_trigger_respects_guard(self) -> None:
        allow = True
        sm: StateMachine[Light, Signal] = StateMachine(
            initial_state=Light.RED,
            transitions=[
                Transition(Light.RED, Signal.ADVANCE, Light.GREEN, guard=lambda: allow),
            ],
        )
        assert sm.can_trigger(Signal.ADVANCE) is True
        allow = False
        assert sm.can_trigger(Signal.ADVANCE) is False


class TestActions:

    def test_action_called_on_valid_transition(self) -> None:
        calls: list[str] = []

        sm: StateMachine[Light, Signal] = StateMachine(
            initial_state=Light.RED,
            transitions=[
                Transition(
                    Light.RED, Signal.ADVANCE, Light.GREEN,
                    action=lambda: calls.append("fired"),
                ),
            ],
        )
        sm.trigger(Signal.ADVANCE)
        assert calls == ["fired"]

    def test_action_sees_pre_transition_state(self) -> None:
        seen: list[Light] = []

        sm: StateMachine[Light, Signal] = StateMachine(
            initial_state=Light.RED,
            transitions=[
                Transition(
                    Light.RED, Signal.ADVANCE, Light.GREEN,
                    action=lambda: seen.append(sm.state),  # captures sm
                ),
            ],
        )
        sm.trigger(Signal.ADVANCE)
        # Action called before state update, so sees RED
        assert seen == [Light.RED]
        assert sm.state == Light.GREEN

    def test_action_not_called_on_blocked_guard(self) -> None:
        calls: list[str] = []

        sm: StateMachine[Light, Signal] = StateMachine(
            initial_state=Light.RED,
            transitions=[
                Transition(
                    Light.RED, Signal.ADVANCE, Light.GREEN,
                    guard=lambda: False,
                    action=lambda: calls.append("should not fire"),
                ),
            ],
        )
        sm.trigger(Signal.ADVANCE)
        assert calls == []

    def test_action_not_called_on_missing_arc(self) -> None:
        calls: list[str] = []
        sm: StateMachine[Light, Signal] = StateMachine(
            initial_state=Light.RED,
            transitions=[
                Transition(
                    Light.RED, Signal.ADVANCE, Light.GREEN,
                    action=lambda: calls.append("fired"),
                ),
            ],
        )
        sm.trigger(Signal.RESET)   # no arc
        assert calls == []


# ─── Order FSM ────────────────────────────────────────────────────────────────


class TestOrderFSM:

    def test_happy_path_market_buy(self) -> None:
        sm = build_order_fsm(OrderState.PENDING)
        assert sm.trigger(OrderEvent.SUBMIT)
        assert sm.trigger(OrderEvent.ACCEPT)
        assert sm.trigger(OrderEvent.FULL_FILL)
        assert sm.state == OrderState.FILLED

    def test_partial_then_full_fill(self) -> None:
        sm = build_order_fsm(OrderState.PENDING)
        sm.trigger(OrderEvent.SUBMIT)
        sm.trigger(OrderEvent.ACCEPT)
        sm.trigger(OrderEvent.PARTIAL_FILL)
        assert sm.state == OrderState.PARTIAL
        sm.trigger(OrderEvent.FULL_FILL)
        assert sm.state == OrderState.FILLED

    def test_cancel_from_accepted(self) -> None:
        sm = build_order_fsm(OrderState.PENDING)
        sm.trigger(OrderEvent.SUBMIT)
        sm.trigger(OrderEvent.ACCEPT)
        sm.trigger(OrderEvent.CANCEL)
        assert sm.state == OrderState.CANCELLED

    def test_cancel_from_partial(self) -> None:
        sm = build_order_fsm(OrderState.PENDING)
        sm.trigger(OrderEvent.SUBMIT)
        sm.trigger(OrderEvent.ACCEPT)
        sm.trigger(OrderEvent.PARTIAL_FILL)
        sm.trigger(OrderEvent.CANCEL)
        assert sm.state == OrderState.CANCELLED

    def test_reject_from_pending(self) -> None:
        sm = build_order_fsm(OrderState.PENDING)
        sm.trigger(OrderEvent.REJECT)
        assert sm.state == OrderState.REJECTED

    def test_reject_from_submitted(self) -> None:
        sm = build_order_fsm(OrderState.PENDING)
        sm.trigger(OrderEvent.SUBMIT)
        sm.trigger(OrderEvent.REJECT)
        assert sm.state == OrderState.REJECTED

    def test_expire_from_accepted(self) -> None:
        sm = build_order_fsm(OrderState.PENDING)
        sm.trigger(OrderEvent.SUBMIT)
        sm.trigger(OrderEvent.ACCEPT)
        sm.trigger(OrderEvent.EXPIRE)
        assert sm.state == OrderState.EXPIRED

    def test_expire_from_partial(self) -> None:
        sm = build_order_fsm(OrderState.PENDING)
        sm.trigger(OrderEvent.SUBMIT)
        sm.trigger(OrderEvent.ACCEPT)
        sm.trigger(OrderEvent.PARTIAL_FILL)
        sm.trigger(OrderEvent.EXPIRE)
        assert sm.state == OrderState.EXPIRED

    def test_no_transition_from_filled(self) -> None:
        sm = build_order_fsm(OrderState.FILLED)
        assert sm.trigger(OrderEvent.CANCEL) is False
        assert sm.trigger(OrderEvent.FULL_FILL) is False
        assert sm.state == OrderState.FILLED

    def test_no_transition_from_rejected(self) -> None:
        sm = build_order_fsm(OrderState.REJECTED)
        assert sm.trigger(OrderEvent.SUBMIT) is False
        assert sm.state == OrderState.REJECTED

    def test_crash_recovery_reset(self) -> None:
        """Simulates crash during SUBMITTED state; replay sets SUBMITTED then continues."""
        sm = build_order_fsm(OrderState.PENDING)
        sm.trigger(OrderEvent.SUBMIT)   # crash here
        # On recovery: we know the order was SUBMITTED, so reset to that
        sm.reset(OrderState.SUBMITTED)
        assert sm.state == OrderState.SUBMITTED
        # Can continue from SUBMITTED
        sm.trigger(OrderEvent.ACCEPT)
        assert sm.state == OrderState.ACCEPTED

    def test_multiple_partial_fills(self) -> None:
        sm = build_order_fsm(OrderState.PENDING)
        sm.trigger(OrderEvent.SUBMIT)
        sm.trigger(OrderEvent.ACCEPT)
        sm.trigger(OrderEvent.PARTIAL_FILL)
        sm.trigger(OrderEvent.PARTIAL_FILL)  # stays PARTIAL
        assert sm.state == OrderState.PARTIAL
        sm.trigger(OrderEvent.FULL_FILL)
        assert sm.state == OrderState.FILLED


# ─── BacktestClock ────────────────────────────────────────────────────────────


class TestBacktestClock:

    def _et_dt(self, year: int, month: int, day: int, hour: int = 10, minute: int = 0) -> datetime:
        from core.clock import ET
        return datetime(year, month, day, hour, minute, tzinfo=ET)

    def test_initial_time(self) -> None:
        dt = self._et_dt(2026, 1, 5, 10)
        clock = BacktestClock(dt)
        assert clock.now_et().hour == 10

    def test_advance(self) -> None:
        dt = self._et_dt(2026, 1, 5, 10)
        clock = BacktestClock(dt)
        clock.advance(timedelta(hours=2))
        assert clock.now_et().hour == 12

    def test_set(self) -> None:
        start = self._et_dt(2026, 1, 5, 10)
        clock = BacktestClock(start)
        new_dt = self._et_dt(2026, 6, 15, 9, 30)
        clock.set(new_dt)
        assert clock.today_et() == new_dt.date()

    def test_naive_datetime_raises(self) -> None:
        with pytest.raises(ValueError):
            BacktestClock(datetime(2026, 1, 5, 10))

    def test_market_open_during_session(self) -> None:
        from core.clock import ET
        clock = BacktestClock(datetime(2026, 1, 5, 10, 0, tzinfo=ET))
        assert clock.market_open() is True

    def test_market_closed_before_open(self) -> None:
        from core.clock import ET
        clock = BacktestClock(datetime(2026, 1, 5, 9, 0, tzinfo=ET))
        assert clock.market_open() is False

    def test_market_closed_after_close(self) -> None:
        from core.clock import ET
        clock = BacktestClock(datetime(2026, 1, 5, 16, 1, tzinfo=ET))
        assert clock.market_open() is False

    def test_market_closed_on_weekend(self) -> None:
        from core.clock import ET
        # 2026-01-03 is a Saturday
        clock = BacktestClock(datetime(2026, 1, 3, 10, 0, tzinfo=ET))
        assert clock.market_open() is False

    def test_market_closed_on_nyse_holiday(self) -> None:
        from core.clock import ET
        # 2026-01-01 = New Year's Day
        clock = BacktestClock(datetime(2026, 1, 1, 10, 0, tzinfo=ET))
        assert clock.market_open() is False

    def test_is_trading_day_weekday(self) -> None:
        from core.clock import ET
        clock = BacktestClock(datetime(2026, 1, 5, 12, 0, tzinfo=ET))  # Monday
        assert clock.is_trading_day() is True

    def test_is_trading_day_false_on_holiday(self) -> None:
        from datetime import date

        from core.clock import ET
        clock = BacktestClock(datetime(2026, 7, 3, 12, 0, tzinfo=ET))
        assert clock.is_trading_day(date(2026, 7, 3)) is False


# ─── EventBus ─────────────────────────────────────────────────────────────────


class TestEventBus:

    def test_publish_reaches_subscriber(self) -> None:
        bus = EventBus()
        received: list[object] = []
        bus.subscribe("order.state_changed", lambda e: received.append(e))

        from core.types import OrderState  # noqa: PLC0415
        evt = OrderStateChangedEvent(
            old_state=OrderState.PENDING,
            new_state=OrderState.SUBMITTED,
        )
        bus.publish(evt)
        assert len(received) == 1
        assert received[0] is evt

    def test_publish_does_not_reach_wrong_subscriber(self) -> None:
        bus = EventBus()
        received: list[object] = []
        bus.subscribe("intent.rejected", lambda e: received.append(e))
        bus.publish(OrderStateChangedEvent())
        assert received == []

    def test_multiple_subscribers(self) -> None:
        bus = EventBus()
        log1: list[object] = []
        log2: list[object] = []
        bus.subscribe("intent.rejected", lambda e: log1.append(e))
        bus.subscribe("intent.rejected", lambda e: log2.append(e))
        bus.publish(IntentRejectedEvent(reason="test"))
        assert len(log1) == 1
        assert len(log2) == 1

    def test_unsubscribe(self) -> None:
        bus = EventBus()
        received: list[object] = []
        handler = lambda e: received.append(e)  # noqa: E731
        bus.subscribe("intent.rejected", handler)
        bus.unsubscribe("intent.rejected", handler)
        bus.publish(IntentRejectedEvent())
        assert received == []

    def test_wildcard_subscriber(self) -> None:
        bus = EventBus()
        all_events: list[object] = []
        bus.subscribe_all(lambda e: all_events.append(e))
        bus.publish_all(IntentRejectedEvent(reason="x"))
        bus.publish_all(OrderStateChangedEvent())
        assert len(all_events) == 2

    def test_specific_and_wildcard_both_fire(self) -> None:
        bus = EventBus()
        specific: list[object] = []
        wildcard: list[object] = []
        bus.subscribe("intent.rejected", lambda e: specific.append(e))
        bus.subscribe_all(lambda e: wildcard.append(e))
        evt = IntentRejectedEvent(reason="both")
        bus.publish_all(evt)
        assert len(specific) == 1
        assert len(wildcard) == 1

    def test_publish_without_subscribers_is_noop(self) -> None:
        bus = EventBus()
        # Should not raise
        bus.publish(KillSwitchTrippedEvent(new_state=KillSwitchState.DAILY_LOSS))
