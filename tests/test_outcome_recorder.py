"""Tests for OutcomeRecorder — the bus subscriber that closes the loop
between OMS terminal states and per-agent intent_log.outcome rows.

Without this, agent prompts show every prior intent as `→ None`, even when
the broker rejected it. See diagnosis: BTC TIF rejections went undetected
by Haiku for 4 days because of this gap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from agents.memory import AgentMemory
from agents.outcome_recorder import OutcomeRecorder
from core.clock import BacktestClock
from core.events import EventBus
from core.types import AgentId, OrderSide
from execution.fake_broker import FakeBroker, FillMode, make_market_order
from execution.oms import OMS
from execution.oms_store import OMSStore


# ─── Fixture ──────────────────────────────────────────────────────────────────


def _wire(tmp_path: Path, fill_mode: FillMode = FillMode.INSTANT) -> tuple[
    OMS, FakeBroker, EventBus, dict[AgentId, AgentMemory], OutcomeRecorder
]:
    clock = BacktestClock(datetime(2026, 5, 1, 14, 0, tzinfo=UTC))
    broker = FakeBroker(clock=clock, fill_mode=fill_mode, starting_cash=Decimal("30000"))
    broker.set_price("SPY", Decimal("450"))
    store = OMSStore(tmp_path / "oms.db")
    bus = EventBus()
    oms = OMS(broker=broker, store=store, bus=bus, clock=clock)
    memories = {
        AgentId.HAIKU: AgentMemory(tmp_path / "haiku.db", AgentId.HAIKU),
        AgentId.SONNET: AgentMemory(tmp_path / "sonnet.db", AgentId.SONNET),
    }
    recorder = OutcomeRecorder(memories, oms, bus)
    return oms, broker, bus, memories, recorder


def _seed_intent(memory: AgentMemory, intent_id: str, symbol: str = "SPY") -> None:
    """Mirror the agent's record_intent so we have a row for record_outcome
    to UPDATE."""
    memory.record_intent(
        intent_id=intent_id,
        symbol=symbol,
        action="buy",
        conviction=7,
        rationale="test",
        ts=datetime(2026, 5, 1, 14, 0, tzinfo=UTC),
    )


def _outcome(memory: AgentMemory, intent_id: str) -> str | None:
    rows = memory.recent_intents_rows(n=10)
    for row in rows:
        if row["intent_id"] == intent_id:
            return row["outcome"]  # type: ignore[return-value]
    return None


# ─── Tests ────────────────────────────────────────────────────────────────────


class TestFullFill:

    def test_instant_full_fill_records_filled(self, tmp_path: Path) -> None:
        oms, _broker, _bus, memories, _rec = _wire(tmp_path)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("5"),
            agent_id=AgentId.HAIKU,
        )
        _seed_intent(memories[AgentId.HAIKU], str(order.intent_id))
        oms.submit_order(order)
        assert _outcome(memories[AgentId.HAIKU], str(order.intent_id)) == "filled"


class TestPartialFill:

    def test_partial_fill_does_not_set_outcome_yet(self, tmp_path: Path) -> None:
        oms, broker, _bus, memories, _rec = _wire(tmp_path, FillMode.MANUAL)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"),
            agent_id=AgentId.HAIKU,
        )
        _seed_intent(memories[AgentId.HAIKU], str(order.intent_id))
        oms.submit_order(order)
        broker.force_partial_fill(order.id, qty=Decimal("4"), price=Decimal("450"))
        assert _outcome(memories[AgentId.HAIKU], str(order.intent_id)) is None
        # Now complete it.
        broker.force_full_fill(order.id, price=Decimal("450"))
        assert _outcome(memories[AgentId.HAIKU], str(order.intent_id)) == "filled"


class TestBrokerRejection:

    def test_broker_reject_records_rejected_with_reason(self, tmp_path: Path) -> None:
        oms, _broker, _bus, memories, _rec = _wire(tmp_path, FillMode.REJECT)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("5"),
            agent_id=AgentId.HAIKU,
        )
        _seed_intent(memories[AgentId.HAIKU], str(order.intent_id))
        oms.submit_order(order)
        outcome = _outcome(memories[AgentId.HAIKU], str(order.intent_id))
        assert outcome is not None
        assert outcome.startswith("rejected:")


class TestCancellation:

    def test_cancel_records_cancelled(self, tmp_path: Path) -> None:
        oms, _broker, _bus, memories, _rec = _wire(tmp_path, FillMode.MANUAL)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("5"),
            agent_id=AgentId.HAIKU,
        )
        _seed_intent(memories[AgentId.HAIKU], str(order.intent_id))
        oms.submit_order(order)
        oms.cancel_order(order.id)
        outcome = _outcome(memories[AgentId.HAIKU], str(order.intent_id))
        assert outcome is not None
        assert outcome.startswith("cancelled")


class TestDirectRecord:

    def test_record_writes_outcome(self, tmp_path: Path) -> None:
        _oms, _broker, _bus, memories, recorder = _wire(tmp_path)
        intent_id = str(uuid4())
        _seed_intent(memories[AgentId.HAIKU], intent_id)
        recorder.record(intent_id, AgentId.HAIKU, "vetoed:gross_cap")
        assert _outcome(memories[AgentId.HAIKU], intent_id) == "vetoed:gross_cap"

    def test_record_truncates_long_outcomes(self, tmp_path: Path) -> None:
        _oms, _broker, _bus, memories, recorder = _wire(tmp_path)
        intent_id = str(uuid4())
        _seed_intent(memories[AgentId.HAIKU], intent_id)
        long_reason = "rejected:" + "x" * 500
        recorder.record(intent_id, AgentId.HAIKU, long_reason)
        out = _outcome(memories[AgentId.HAIKU], intent_id)
        assert out is not None
        assert len(out) <= 120

    def test_record_for_unknown_agent_is_noop(self, tmp_path: Path) -> None:
        _oms, _broker, _bus, _memories, recorder = _wire(tmp_path)
        # Should not raise, just log.
        recorder.record(str(uuid4()), AgentId.OPUS, "filled")


class TestRoutesToCorrectAgent:

    def test_haiku_intent_does_not_leak_into_sonnet(self, tmp_path: Path) -> None:
        oms, _broker, _bus, memories, _rec = _wire(tmp_path)
        order = make_market_order(
            symbol="SPY", side=OrderSide.BUY, qty=Decimal("3"),
            agent_id=AgentId.HAIKU,
        )
        _seed_intent(memories[AgentId.HAIKU], str(order.intent_id))
        # Sonnet has its own seeded intent that should not be touched.
        sonnet_intent = str(uuid4())
        _seed_intent(memories[AgentId.SONNET], sonnet_intent, symbol="QQQ")
        oms.submit_order(order)
        assert _outcome(memories[AgentId.HAIKU], str(order.intent_id)) == "filled"
        assert _outcome(memories[AgentId.SONNET], sonnet_intent) is None
