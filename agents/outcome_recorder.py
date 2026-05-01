"""Routes terminal-state intent outcomes back to per-agent SQLite memory.

Without this wiring every row in `intent_log.outcome` stays NULL forever, so
the agents' `recent_intents_summary()` shows every prior intent — filled,
rejected, vetoed alike — as `→ None`. The LLM can't tell a successful
submission from a silently-rejected one and stops re-trying valid signals
(e.g. the BTC TIF rejection masked from Haiku for 4 days).

Subscribes to:
  * order.placed         — caches order_id → (intent_id, agent_id)
  * fill.received        — on full fill, records "filled"
  * order.state_changed  — on REJECTED / CANCELLED / EXPIRED, records the
                           reason fetched from the OMS Order snapshot

For the two pre-OMS reject paths (RiskGate veto, planner sub-min), callers
should invoke `record()` directly — those intents never reach the OMS so
no event is published.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.events import (
    EventBus,
    FillReceivedEvent,
    OrderPlacedEvent,
    OrderStateChangedEvent,
)
from core.types import AgentId, IntentId, OrderId, OrderState

if TYPE_CHECKING:
    from agents.memory import AgentMemory
    from execution.oms import OMS

logger = logging.getLogger(__name__)

# Outcome string max length. Keeps long broker rejection messages from
# blowing past the rationale-truncation budget in `recent_intents_summary`.
_MAX_OUTCOME_LEN = 120

_TERMINAL_FAILURE_OUTCOMES: dict[OrderState, str] = {
    OrderState.REJECTED:  "rejected",
    OrderState.CANCELLED: "cancelled",
    OrderState.EXPIRED:   "expired",
}


class OutcomeRecorder:
    """Wires bus events into per-agent intent_log.outcome updates."""

    def __init__(
        self,
        memories: dict[AgentId, AgentMemory],
        oms: OMS,
        bus: EventBus,
    ) -> None:
        self._memories = memories
        self._oms = oms
        self._intent_index: dict[OrderId, tuple[IntentId, AgentId]] = {}

        bus.subscribe("order.placed", self._on_order_placed)
        bus.subscribe("fill.received", self._on_fill)
        bus.subscribe("order.state_changed", self._on_state_changed)

    # ── Public: direct call path for pre-OMS rejections ──────────────────────

    def record(self, intent_id: IntentId, agent_id: AgentId, outcome: str) -> None:
        """Synchronously write an outcome for an intent that never produced
        an Order (RiskGate veto, planner returned None)."""
        self._write(intent_id, agent_id, outcome)

    # ── Bus handlers ─────────────────────────────────────────────────────────

    def _on_order_placed(self, event: OrderPlacedEvent) -> None:
        order = event.order
        self._intent_index[order.id] = (order.intent_id, order.agent_id)

    def _on_fill(self, event: FillReceivedEvent) -> None:
        # Only terminal full fills become an outcome. Partial fills leave the
        # row as NULL until the order completes — the LLM should treat a
        # partially-filled in-flight order as "still working."
        if event.fill.is_partial:
            return
        ref = self._intent_index.pop(event.fill.order_id, None)
        if ref is None:
            # Fallback: agent_id is on the Fill itself; we still need
            # intent_id, which only the Order carries. Skip silently if we
            # never saw the OrderPlaced (recovery / cold-cache scenarios).
            order = self._oms.get_order(event.fill.order_id)
            if order is None:
                return
            ref = (order.intent_id, order.agent_id)
        intent_id, agent_id = ref
        self._write(intent_id, agent_id, "filled")

    def _on_state_changed(self, event: OrderStateChangedEvent) -> None:
        outcome_kind = _TERMINAL_FAILURE_OUTCOMES.get(event.new_state)
        if outcome_kind is None:
            return
        ref = self._intent_index.pop(event.order_id, None)
        order = self._oms.get_order(event.order_id)
        if ref is None and order is None:
            return
        if ref is None:
            assert order is not None
            ref = (order.intent_id, order.agent_id)
        intent_id, agent_id = ref
        # Append the broker / OMS reason if available so the LLM context
        # shows e.g. "rejected:invalid crypto time_in_force…".
        reason = (order.rejection_reason if order is not None else None) or ""
        outcome = f"{outcome_kind}:{reason}" if reason else outcome_kind
        self._write(intent_id, agent_id, outcome)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _write(self, intent_id: IntentId, agent_id: AgentId, outcome: str) -> None:
        mem = self._memories.get(agent_id)
        if mem is None:
            logger.warning(
                "OutcomeRecorder: no memory for agent %s (intent %s)",
                agent_id, intent_id,
            )
            return
        try:
            mem.record_outcome(intent_id, outcome[:_MAX_OUTCOME_LEN])
        except Exception:
            logger.exception(
                "OutcomeRecorder: failed to record outcome for intent %s",
                intent_id,
            )
