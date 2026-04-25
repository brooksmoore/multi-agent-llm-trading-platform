"""Tests for dashboard/data.py — read-only adapter over OMS, memory, calibration, budget."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from agents.calibration import CalibrationTracker
from agents.memory import AgentMemory
from core.types import AgentId, OrderId, new_id
from dashboard.data import DashboardData
from dashboard.layout import render_full_dashboard
from execution.budget import BudgetLedger
from execution.oms_store import EventKind, OMSStore

_TS = datetime(2026, 4, 25, 17, 0, tzinfo=UTC)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def memory_haiku() -> AgentMemory:
    mem = AgentMemory(":memory:", AgentId.HAIKU)
    yield mem
    mem.close()


@pytest.fixture
def memory_sonnet() -> AgentMemory:
    mem = AgentMemory(":memory:", AgentId.SONNET)
    yield mem
    mem.close()


@pytest.fixture
def calibration() -> CalibrationTracker:
    cal = CalibrationTracker(":memory:")
    yield cal
    cal.close()


@pytest.fixture
def budget(tmp_path: Path) -> BudgetLedger:
    return BudgetLedger(tmp_path / "spend.json", daily_limit=Decimal("0.95"))


@pytest.fixture
def oms_store(tmp_path: Path) -> OMSStore:
    store = OMSStore(tmp_path / "oms.db")
    yield store
    store.close()


# ── DashboardData.top_strip ──────────────────────────────────────────────────


def test_top_strip_with_no_stores_returns_defaults() -> None:
    data = DashboardData()
    m = data.top_strip()
    assert m.day_spend_usd == Decimal("0")
    assert m.spend_pct == 0.0
    assert m.master_capability == Decimal("1.0")
    assert m.halted is False


def test_top_strip_reflects_budget_spend(budget: BudgetLedger) -> None:
    budget.record_spend("haiku", Decimal("0.10"), "morning_brief", _TS)
    data = DashboardData(budget=budget)
    m = data.top_strip()
    assert m.day_spend_usd == Decimal("0.10")
    assert m.spend_pct == pytest.approx(100 * 0.10 / 0.95, rel=0.01)
    assert m.spend_limit_usd == Decimal("0.95")


def test_top_strip_reflects_master_capability_and_regime() -> None:
    data = DashboardData(
        master_capability=Decimal("0.75"),
        regime_label="risk_off",
        halted=True,
    )
    m = data.top_strip()
    assert m.master_capability == Decimal("0.75")
    assert m.regime_label == "risk_off"
    assert m.halted is True


def test_top_strip_heartbeat_age() -> None:
    hb = datetime.now(UTC).replace(microsecond=0)
    data = DashboardData(heartbeat=hb)
    m = data.top_strip()
    assert 0 <= m.heartbeat_age_s <= 5


# ── DashboardData.agent_summary ──────────────────────────────────────────────


def test_agent_summary_returns_recent_intents(memory_haiku: AgentMemory) -> None:
    iid1 = new_id()
    iid2 = new_id()
    memory_haiku.record_intent(iid1, "SPY", "buy", 7, "Trend up.", _TS)
    memory_haiku.record_intent(iid2, "QQQ", "buy", 6, "Tech leads.", _TS)
    data = DashboardData(memories={AgentId.HAIKU: memory_haiku})
    s = data.agent_summary(AgentId.HAIKU)
    assert s.agent_id == "haiku"
    assert len(s.recent_intents) == 2
    syms = {i.symbol for i in s.recent_intents}
    assert syms == {"SPY", "QQQ"}


def test_agent_summary_includes_calibration(
    memory_haiku: AgentMemory, calibration: CalibrationTracker
) -> None:
    calibration.record("id1", "haiku", 8, "win")
    calibration.record("id2", "haiku", 5, "loss")
    data = DashboardData(memories={AgentId.HAIKU: memory_haiku}, calibration=calibration)
    s = data.agent_summary(AgentId.HAIKU)
    assert s.brier_score >= 0.0
    assert isinstance(s.calibration_table, list)


def test_agent_summary_with_no_memory_returns_empty_intents() -> None:
    data = DashboardData()
    s = data.agent_summary(AgentId.HAIKU)
    assert s.recent_intents == []
    assert s.brier_score == 0.0


def test_agent_summary_outcome_passes_through(memory_haiku: AgentMemory) -> None:
    iid = new_id()
    memory_haiku.record_intent(iid, "SPY", "buy", 7, "Trend.", _TS)
    memory_haiku.record_outcome(iid, "win")
    data = DashboardData(memories={AgentId.HAIKU: memory_haiku})
    s = data.agent_summary(AgentId.HAIKU)
    assert s.recent_intents[0].outcome == "win"


# ── DashboardData.recent_intents (cross-agent) ───────────────────────────────


def test_recent_intents_aggregates_across_agents(
    memory_haiku: AgentMemory, memory_sonnet: AgentMemory
) -> None:
    memory_haiku.record_intent(new_id(), "SPY", "buy", 7, "Haiku trend.", _TS)
    memory_sonnet.record_intent(new_id(), "NVDA", "buy", 8, "Factor top.", _TS)
    data = DashboardData(
        memories={AgentId.HAIKU: memory_haiku, AgentId.SONNET: memory_sonnet}
    )
    rows = data.recent_intents(50)
    agents = {r.agent_id for r in rows}
    assert agents == {"haiku", "sonnet"}


def test_recent_intents_respects_n_cap(memory_haiku: AgentMemory) -> None:
    for i in range(20):
        memory_haiku.record_intent(new_id(), f"S{i}", "buy", 5, f"r{i}", _TS)
    data = DashboardData(memories={AgentId.HAIKU: memory_haiku})
    rows = data.recent_intents(5)
    assert len(rows) == 5


# ── DashboardData.recent_fills (OMS) ─────────────────────────────────────────


def test_recent_fills_returns_empty_with_no_oms() -> None:
    assert DashboardData().recent_fills() == []


def test_recent_fills_extracts_fill_events(oms_store: OMSStore) -> None:
    order_id: OrderId = new_id()
    oms_store.append(
        EventKind.FILL_RECEIVED,
        order_id,
        {"symbol": "SPY", "side": "buy", "qty": Decimal("10"), "price": Decimal("420.50")},
        _TS,
    )
    oms_store.append(
        EventKind.ORDER_ACCEPTED,
        order_id,
        {"symbol": "SPY"},  # not a fill — should be ignored
        _TS,
    )
    oms_store.append(
        EventKind.FILL_RECEIVED,
        order_id,
        {"symbol": "SPY", "side": "buy", "qty": Decimal("5"), "price": Decimal("421.00")},
        _TS,
    )

    data = DashboardData(oms_store=oms_store)
    fills = data.recent_fills(10)
    assert len(fills) == 2
    # Most recent first (reversed from monotonic seq)
    assert fills[0].price == Decimal("421.00")
    assert fills[1].price == Decimal("420.50")
    assert all(f.symbol == "SPY" for f in fills)
    assert all(f.side == "buy" for f in fills)


def test_recent_fills_respects_n_cap(oms_store: OMSStore) -> None:
    order_id: OrderId = new_id()
    for i in range(10):
        oms_store.append(
            EventKind.FILL_RECEIVED,
            order_id,
            {"symbol": "SPY", "side": "buy", "qty": Decimal("1"), "price": Decimal(str(420 + i))},
            _TS,
        )
    fills = DashboardData(oms_store=oms_store).recent_fills(3)
    assert len(fills) == 3


# ── DashboardData.spend_breakdown ────────────────────────────────────────────


def test_spend_breakdown_with_no_budget_returns_zeros() -> None:
    s = DashboardData().spend_breakdown()
    assert s.today_total == Decimal("0")
    assert s.by_agent == {}


def test_spend_breakdown_aggregates_by_agent_and_call_type(budget: BudgetLedger) -> None:
    budget.record_spend("haiku", Decimal("0.005"), "morning_brief", _TS)
    budget.record_spend("haiku", Decimal("0.003"), "trend_observe", _TS)
    budget.record_spend("sonnet", Decimal("0.010"), "factor_observe", _TS)
    data = DashboardData(budget=budget)
    s = data.spend_breakdown(fraction_of_day_elapsed=0.5)
    assert s.today_total == pytest.approx(Decimal("0.018"))
    assert s.by_agent["haiku"] == pytest.approx(Decimal("0.008"))
    assert s.by_agent["sonnet"] == pytest.approx(Decimal("0.010"))
    assert "morning_brief" in s.by_call_type
    assert "factor_observe" in s.by_call_type


def test_spend_breakdown_eod_forecast_doubles_at_half_day(budget: BudgetLedger) -> None:
    budget.record_spend("haiku", Decimal("0.10"), "x", _TS)
    s = DashboardData(budget=budget).spend_breakdown(fraction_of_day_elapsed=0.5)
    assert s.eod_forecast == pytest.approx(Decimal("0.20"))


def test_spend_breakdown_caps_forecast_at_2x_limit(budget: BudgetLedger) -> None:
    budget.record_spend("haiku", Decimal("5.00"), "x", _TS)
    s = DashboardData(budget=budget).spend_breakdown(fraction_of_day_elapsed=0.5)
    assert s.eod_forecast <= budget.daily_limit() * Decimal("2")


def test_spend_breakdown_handles_zero_elapsed(budget: BudgetLedger) -> None:
    budget.record_spend("haiku", Decimal("0.05"), "x", _TS)
    s = DashboardData(budget=budget).spend_breakdown(fraction_of_day_elapsed=0.0)
    # Forecast equals current total when elapsed too small
    assert s.eod_forecast == Decimal("0.05")


# ── End-to-end render (catches layout/data-shape mismatches) ─────────────────


def test_render_full_dashboard_with_empty_data_does_not_raise() -> None:
    data = DashboardData()
    component = render_full_dashboard(data)
    assert component is not None


def test_render_full_dashboard_with_populated_data(
    memory_haiku: AgentMemory,
    calibration: CalibrationTracker,
    budget: BudgetLedger,
    oms_store: OMSStore,
) -> None:
    memory_haiku.record_intent(new_id(), "SPY", "buy", 7, "Faber trend.", _TS)
    calibration.record("id1", "haiku", 7, "win")
    budget.record_spend("haiku", Decimal("0.005"), "trend_observe", _TS)
    oms_store.append(
        EventKind.FILL_RECEIVED,
        new_id(),
        {"symbol": "SPY", "side": "buy", "qty": Decimal("10"), "price": Decimal("420.50")},
        _TS,
    )
    data = DashboardData(
        memories={AgentId.HAIKU: memory_haiku},
        calibration=calibration,
        budget=budget,
        oms_store=oms_store,
        master_capability=Decimal("1.0"),
        regime_label="risk_on",
    )
    component = render_full_dashboard(data)
    assert component is not None


def test_budget_reset_if_new_day_does_not_break_dashboard(budget: BudgetLedger) -> None:
    budget.record_spend("haiku", Decimal("0.05"), "x", _TS)
    budget.reset_if_new_day(date(2026, 5, 1))
    s = DashboardData(budget=budget).spend_breakdown()
    assert s.today_total == Decimal("0")
