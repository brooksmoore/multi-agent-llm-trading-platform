"""Smoke test — end-to-end integration of all M9 subsystems.

Exercises the full chain:
  Intent (mocked observe) → RiskGate → accepted list
  + OMS.submit_order → FakeBroker fill → ledger updated
  + HeartbeatWriter tick → file on disk
  + Reconciler.reconcile_once → no false halts on clean state
  + BudgetWatcher.check_once → no spurious trip with headroom
  + App.start() → App.stop() → shutdown summary with correct fields

No Alpaca or Anthropic API calls are made.  All I/O is local tmp.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from app import App
from config.settings import Settings
from core.types import (
    Action,
    AgentId,
    Intent,
    OrderSide,
    Sleeve,
)
from data.market import Bar, Timeframe
from execution.fake_broker import FakeBroker, make_market_order
from ops.heartbeat import HeartbeatWriter
from ops.journal import write_daily_memo, write_weekly_journal

# ── Shared fixtures ───────────────────────────────────────────────────────────


class _StubMD:
    def __init__(self, bars: dict[str, list[Bar]]) -> None:
        self._bars = bars

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

    def get_snapshots(self, symbols: list[str]) -> dict[str, Any]:
        return {s: b[-1] for s, b in self._bars.items() if s in symbols and b}


def _bar(sym: str, price: str = "400.00") -> Bar:
    return Bar(
        symbol=sym,
        timestamp=datetime(2026, 4, 25, 10, 0, tzinfo=UTC),
        open=Decimal(price),
        high=Decimal(price),
        low=Decimal(price),
        close=Decimal(price),
        volume=Decimal("100000"),
    )


def _settings(tmp: Path) -> Settings:
    return Settings(
        alpaca_paper=True,
        alpaca_api_key="x",
        alpaca_secret_key="x",
        anthropic_api_key="x",
        ntfy_topic="",
        master_capability=Decimal("1.0"),
        auto_approve=True,
        daily_spend_cap=Decimal("0.95"),
        data_dir=str(tmp / "data"),
        logs_dir=str(tmp / "logs"),
    )


def _make_app(tmp: Path) -> App:
    broker = FakeBroker()
    md = _StubMD({s: [_bar(s)] for s in ("SPY", "QQQ", "AAPL")})
    return App(
        _settings(tmp),
        broker=broker,
        market_data=md,
        universe=["SPY", "QQQ", "AAPL"],
        run_dashboard=False,
        run_volatility_scanner=False,
        run_recover_on_start=False,
    )


def _stub_intent(agent_id: AgentId, symbol: str = "SPY") -> Intent:
    return Intent(
        id=uuid.uuid4(),
        agent_id=agent_id,
        symbol=symbol,
        action=Action.BUY,
        target_weight=Decimal("0.10"),
        sleeve=Sleeve.EQUITY,
        signal="smoke-test",
        conviction=5,
        rationale="smoke test intent",
        timestamp=datetime.now(UTC),
    )


# ── Smoke tests ───────────────────────────────────────────────────────────────


def test_full_startup_shutdown_cycle(tmp_path: Path) -> None:
    """App starts all subsystems and shuts down cleanly, producing a valid summary."""
    app = _make_app(tmp_path)
    app.start()
    assert app.scheduler.running is True
    app.stop()
    assert app.scheduler.running is False

    shutdowns = list((tmp_path / "logs").glob("shutdown_*.json"))
    assert len(shutdowns) == 1
    summary = json.loads(shutdowns[0].read_text())
    assert summary["kill_switch_state"] == "ok"
    assert summary["open_orders"] == 0
    assert "started_at" in summary and "shutdown_at" in summary


def test_intent_through_riskgate_returns_accepted_list(tmp_path: Path) -> None:
    """A valid intent is approved by RiskGate and returned in accepted list."""
    app = _make_app(tmp_path)
    intent = _stub_intent(AgentId.HAIKU)
    app.haiku.observe = MagicMock(return_value=[intent])  # type: ignore[method-assign]

    accepted = app.dispatch_observation(app.haiku)

    # RiskGate should allow a simple BUY 10% SPY intent in normal regime
    assert any(i.symbol == "SPY" for i in accepted), (
        "Expected SPY intent to be approved; risk gate may be blocking unexpectedly"
    )
    app.stop()


def test_oms_submit_fill_ledger_cycle(tmp_path: Path) -> None:
    """Full OMS → FakeBroker → fill cycle: order submitted and appears in positions."""
    app = _make_app(tmp_path)
    order = make_market_order(
        symbol="SPY",
        side=OrderSide.BUY,
        qty=Decimal("5"),
        agent_id=AgentId.SONNET,
    )
    result = app.oms.submit_order(order)
    assert result.accepted is True

    filled = app.oms.get_order(order.id)
    assert filled is not None

    positions = {p.symbol: p.qty for p in app.broker.list_positions()}
    assert positions.get("SPY") == Decimal("5"), f"Expected 5 SPY after fill, got {positions}"
    app.stop()


def test_heartbeat_file_written_by_tick_once(tmp_path: Path) -> None:
    """HeartbeatWriter.tick_once() writes a parseable JSON file atomically."""
    path = tmp_path / "logs" / "heartbeat.json"
    writer = HeartbeatWriter(path=path, kill=None, interval_secs=60.0)
    writer._started_at = time.monotonic()
    writer.tick_once()

    assert path.exists()
    data = json.loads(path.read_text())
    assert "ts" in data
    assert "uptime_s" in data
    assert data["uptime_s"] >= 0
    # No stale .tmp file left behind
    assert not path.with_suffix(".json.tmp").exists()


def test_heartbeat_writer_starts_and_stops_with_app(tmp_path: Path) -> None:
    """HeartbeatWriter is alive after start() and not after stop()."""
    app = _make_app(tmp_path)
    app.start()
    assert app.heartbeat._thread is not None
    assert app.heartbeat._thread.is_alive()
    app.stop()
    assert not (app.heartbeat._thread is not None and app.heartbeat._thread.is_alive())


def test_reconciler_clean_state_no_halt(tmp_path: Path) -> None:
    """Reconciler.reconcile_once() with an empty OMS + empty broker = no kill-switch trip."""
    app = _make_app(tmp_path)
    from core.types import KillSwitchState

    ts = datetime.now(UTC)
    result = app.reconciler.reconcile_once(ts)
    assert result.position_mismatches == 0
    assert app.kill.state == KillSwitchState.OK, f"Expected kill switch OK but got {app.kill.state}"
    app.stop()


def test_budget_watcher_no_trip_with_headroom(tmp_path: Path) -> None:
    """BudgetWatcher.check_once() on a fresh ledger with full headroom stays OK."""
    from core.types import KillSwitchState

    app = _make_app(tmp_path)
    app.budget_watcher.check_once()
    assert app.kill.state == KillSwitchState.OK
    app.stop()


def test_multiple_agents_dispatched_independently(tmp_path: Path) -> None:
    """All three agents can be dispatched in one cycle without interference."""
    app = _make_app(tmp_path)
    for agent in (app.haiku, app.sonnet, app.opus):
        agent.observe = MagicMock(return_value=[])  # type: ignore[method-assign]

    app.dispatch_observation(app.haiku)
    app.dispatch_observation(app.sonnet)
    app.dispatch_observation(app.opus)

    app.haiku.observe.assert_called_once()
    app.sonnet.observe.assert_called_once()
    app.opus.observe.assert_called_once()
    app.stop()


def test_journal_weekly_and_daily_written_to_logs_dir(tmp_path: Path) -> None:
    """Journal helpers write to the expected paths inside logs/."""
    logs = tmp_path / "logs"
    from datetime import date

    weekly = write_weekly_journal("# Week 17\n- All clear.", date(2026, 4, 25), logs)
    daily = write_daily_memo("Haiku memo for the day", AgentId.HAIKU, date(2026, 4, 25), logs)

    assert weekly.exists() and weekly.read_text().startswith("# Week 17")
    assert daily.exists() and daily.read_text() == "Haiku memo for the day"


def test_no_real_money_path_in_settings(tmp_path: Path) -> None:
    """app.py refuses to build with alpaca_paper=False."""
    settings = _settings(tmp_path)
    settings = settings.model_copy(update={"alpaca_paper": False})
    with pytest.raises(RuntimeError, match="alpaca_paper=False"):
        App(
            settings,
            broker=None,  # triggers _build_alpaca_broker which raises on paper=False
            market_data=_StubMD({}),
            universe=[],
        )


def test_macro_calendar_loads_without_error(tmp_path: Path) -> None:
    """YAML macro calendar loads and contains at least one event."""
    app = _make_app(tmp_path)
    assert isinstance(app._macro_calendar, list)
    # config/macro_events.yaml has events for May–Jul 2026
    assert len(app._macro_calendar) >= 1, "macro calendar appears empty — check YAML"
    app.stop()
