"""End-to-end lifecycle test for app.py — start, dispatch, shutdown.

Uses FakeBroker + a stub MarketData so no Alpaca / Anthropic calls happen.
The LLMClient inside each agent is replaced via attribute injection so that
agent.observe() returns deterministic stub intents without an API key.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app import App
from config.settings import Settings
from core.types import Action, AgentId, Intent, Sleeve, new_id
from data.market import Bar, Timeframe
from execution.fake_broker import FakeBroker

# ── Stubs ─────────────────────────────────────────────────────────────────────


class StubMarketData:
    """Minimal MarketData impl: returns a pre-seeded list of bars per symbol."""

    def __init__(self, bars_by_symbol: dict[str, list[Bar]]) -> None:
        self._bars = bars_by_symbol

    def get_bars(
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
        timeframe: Timeframe = Timeframe.DAY,
    ) -> list[Bar]:
        return list(self._bars.get(symbol, []))

    def get_latest_bar(self, symbol: str) -> Bar | None:
        bars = self._bars.get(symbol)
        return bars[-1] if bars else None

    def get_latest_quote(self, symbol: str) -> None:
        return None

    def get_snapshots(self, symbols: list[str]) -> dict[str, Bar]:
        out = {}
        for sym in symbols:
            bar = self.get_latest_bar(sym)
            if bar is not None:
                out[sym] = bar
        return out


def _make_bar(symbol: str, ts: datetime, close: str = "100.0") -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=ts,
        open=Decimal(close), high=Decimal(close), low=Decimal(close),
        close=Decimal(close), volume=Decimal("1000"),
    )


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        alpaca_paper=True,
        alpaca_api_key="dummy",
        alpaca_secret_key="dummy",
        anthropic_api_key="dummy",
        ntfy_topic="",
        master_capability=Decimal("1.0"),
        auto_approve=True,
        daily_spend_cap=Decimal("0.95"),
        data_dir=str(tmp_path / "data"),
        logs_dir=str(tmp_path / "logs"),
    )


def _build_app(tmp_path: Path, *, broker: FakeBroker | None = None) -> App:
    settings = _make_settings(tmp_path)
    broker = broker if broker is not None else FakeBroker()
    bars = {sym: [_make_bar(sym, datetime(2026, 4, 25, 10, 0, tzinfo=UTC))]
            for sym in ("SPY", "QQQ", "AAPL")}
    md = StubMarketData(bars)
    return App(
        settings,
        broker=broker,
        market_data=md,
        universe=list(bars.keys()),
        run_dashboard=False,
        run_volatility_scanner=False,
        run_recover_on_start=False,
    )


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_app_constructs_without_side_effects(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    # Singletons exist
    assert app.oms is not None
    assert app.risk is not None
    assert app.budget is not None
    assert app.haiku.agent_id == AgentId.HAIKU
    assert app.sonnet.agent_id == AgentId.SONNET
    assert app.opus.agent_id == AgentId.OPUS
    # Scheduler not started yet
    assert app.scheduler.running is False
    app.stop()


def test_app_start_then_stop_writes_shutdown_summary(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    app.start()
    assert app.scheduler.running is True
    app.stop()
    assert app.scheduler.running is False
    # Shutdown summary file
    shutdowns = list((tmp_path / "logs").glob("shutdown_*.json"))
    assert len(shutdowns) == 1
    import json
    summary = json.loads(shutdowns[0].read_text())
    assert "shutdown_at" in summary
    assert summary["kill_switch_state"] == "ok"


def test_build_agent_state_populates_bars_positions_and_account(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    state = app.build_agent_state(symbols=["SPY", "QQQ"])
    assert "SPY" in state.bars_by_symbol
    assert "QQQ" in state.bars_by_symbol
    assert len(state.bars_by_symbol["SPY"]) == 1
    assert state.kill_switch_state == app.kill.state
    assert state.master_capability == Decimal("1.0")
    app.stop()


def test_dispatch_observation_calls_observe_on_each_agent(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    # Replace each agent's observe to a stub returning []
    for agent in (app.haiku, app.sonnet, app.opus):
        agent.observe = MagicMock(return_value=[])  # type: ignore[method-assign]

    # Dispatch all three
    assert app.dispatch_observation(app.haiku) == []
    assert app.dispatch_observation(app.sonnet) == []
    assert app.dispatch_observation(app.opus) == []

    app.haiku.observe.assert_called_once()
    app.sonnet.observe.assert_called_once()
    app.opus.observe.assert_called_once()
    app.stop()


def test_dispatch_observation_swallows_agent_exceptions(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    app.haiku.observe = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    intents = app.dispatch_observation(app.haiku)
    assert intents == []
    app.stop()


def test_budget_exhausted_skips_non_haiku_agents(tmp_path: Path) -> None:
    """Per blueprint §5 Layer 3: BUDGET_EXHAUSTED degrades to Haiku-only mode."""
    app = _build_app(tmp_path)
    app.kill.trip_budget_exhausted()
    app.haiku.observe = MagicMock(return_value=[])  # type: ignore[method-assign]
    app.sonnet.observe = MagicMock(return_value=[])  # type: ignore[method-assign]

    app.dispatch_observation(app.haiku)
    app.dispatch_observation(app.sonnet)

    app.haiku.observe.assert_called_once()  # Haiku still runs
    app.sonnet.observe.assert_not_called()  # Sonnet skipped
    app.stop()


def test_stop_is_idempotent(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    app.start()
    app.stop()
    app.stop()  # second call is a no-op


def test_macro_calendar_loaded(tmp_path: Path) -> None:
    app = _build_app(tmp_path)
    # The yaml ships with at least 1 event; if missing, fall back to []
    assert isinstance(app._macro_calendar, list)
    app.stop()


def test_dispatch_observation_submits_order_via_planner(tmp_path: Path) -> None:
    """Integration: intent → RiskGate → ExecutionPlanner → OMS → FakeBroker fill.

    Verifies the full wiring added in M10 sub-task integration commit:
    dispatch_observation() now routes approved intents through the planner and
    submits them to OMS, which fills them via FakeBroker.
    """
    broker = FakeBroker()
    app = _build_app(tmp_path, broker=broker)

    # Give the app enough bars so the vol-snapshot has a valid mark price.
    ts = datetime(2026, 4, 25, 10, 0, tzinfo=UTC)
    bars = {
        "SPY": [_make_bar("SPY", ts, "500.00")],
        "QQQ": [_make_bar("QQQ", ts, "450.00")],
        "AAPL": [_make_bar("AAPL", ts, "200.00")],
    }
    app.market_data = StubMarketData(bars)  # type: ignore[assignment]
    app.universe = list(bars.keys())

    # Stub Sonnet.observe to return a deterministic BUY intent for SPY.
    spy_intent = Intent(
        id=new_id(),
        agent_id=AgentId.SONNET,
        symbol="SPY",
        action=Action.BUY,
        target_weight=Decimal("0.10"),
        sleeve=Sleeve.EQUITY,
        signal="test-signal",
        conviction=7,
        rationale="integration smoke test",
        timestamp=ts,
    )
    app.sonnet.observe = MagicMock(return_value=[spy_intent])  # type: ignore[method-assign]

    result = app.dispatch_observation(app.sonnet)

    # Intent was accepted and routed to OMS.
    assert len(result) == 1
    assert result[0].symbol == "SPY"

    # OMS has at least one order for SPY.
    all_orders = app.oms.list_orders()
    spy_orders = [o for o in all_orders if o.symbol == "SPY"]
    assert len(spy_orders) >= 1, "Expected at least one SPY order in OMS"
    assert spy_orders[0].agent_id == AgentId.SONNET

    app.stop()


@pytest.mark.parametrize("override_field", ["alpaca_api_key", "anthropic_api_key"])
def test_app_constructs_with_empty_credentials(tmp_path: Path, override_field: str) -> None:
    """Empty creds shouldn't crash construction — only API calls do."""
    settings = _make_settings(tmp_path)
    setattr(settings, override_field, "")
    bars = {"SPY": [_make_bar("SPY", datetime(2026, 4, 25, 10, 0, tzinfo=UTC))]}
    app = App(
        settings,
        broker=FakeBroker(),
        market_data=StubMarketData(bars),
        universe=["SPY"],
        run_dashboard=False,
        run_volatility_scanner=False,
        run_recover_on_start=False,
    )
    assert app.budget is not None
    app.stop()
