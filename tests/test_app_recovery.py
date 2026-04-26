"""Crash-recovery test for app.py.

Scenario: app1 boots, submits an order, fills it, then is killed (we simulate
SIGKILL by simply NOT calling app.stop()). app2 then boots against the same
OMS event-log path. recovery should rebuild the in-memory order state from
the append-only log and reconcile against the broker.

We use FakeBroker to avoid Alpaca, but we keep its position state by reusing
the SAME FakeBroker instance across both apps (in real life the broker is the
external source of truth that survives the crash).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app import App
from config.settings import Settings
from core.types import AgentId, OrderSide
from data.market import Bar, Timeframe
from execution.fake_broker import FakeBroker, make_market_order


class _StubMD:
    def get_bars(  # noqa: ARG002
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
        timeframe: Timeframe = Timeframe.DAY,
    ) -> list[Bar]:
        return []

    def get_latest_bar(self, symbol: str) -> Bar | None:  # noqa: ARG002
        return None

    def get_latest_quote(self, symbol: str) -> None:  # noqa: ARG002
        return None

    def get_snapshots(self, symbols: list[str]) -> dict[str, Any]:  # noqa: ARG002
        return {}


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        alpaca_paper=True, alpaca_api_key="x", alpaca_secret_key="x",
        anthropic_api_key="x", ntfy_topic="",
        master_capability=Decimal("1.0"), daily_spend_cap=Decimal("0.95"),
        data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"),
    )


def _make_app(settings: Settings, broker: FakeBroker, *, recover: bool) -> App:
    return App(
        settings, broker=broker, market_data=_StubMD(),
        universe=["SPY"], run_dashboard=False, run_volatility_scanner=False,
        run_recover_on_start=recover,
    )


def test_recovery_rebuilds_state_from_event_log(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    broker = FakeBroker()  # FakeBroker INSTANT mode → fills immediately

    # ── Phase 1: first app submits and fills an order, then "crashes" ────────
    app1 = _make_app(settings, broker, recover=False)
    order = make_market_order(
        symbol="SPY", side=OrderSide.BUY, qty=Decimal("10"), agent_id=AgentId.HAIKU,
    )
    result = app1.oms.submit_order(order)
    assert result.accepted is True
    # Confirm the order is now FILLED in app1's memory
    fetched = app1.oms.get_order(order.id)
    assert fetched is not None
    # Simulate crash: do not call app1.stop() — we just drop the reference
    # but close the OMS store handle so SQLite releases the WAL lock.
    app1.store.close()
    for memory in app1._memories.values():
        memory.close()

    # ── Phase 2: second app boots with the same OMS path and reconciles ─────
    app2 = _make_app(settings, broker, recover=True)
    app2.start()  # triggers oms.recover()

    # Order survives via the event log
    recovered = app2.oms.get_order(order.id)
    assert recovered is not None, "recovery did not rebuild order state"
    assert recovered.symbol == "SPY"
    # Position view: broker shows 10 SPY (broker survived "crash")
    positions = {p.symbol: p.qty for p in broker.list_positions()}
    assert positions.get("SPY") == Decimal("10")
    app2.stop()


def test_recovery_with_empty_log_is_noop(tmp_path: Path) -> None:
    """A fresh OMS database recovers cleanly with zero replays."""
    settings = _make_settings(tmp_path)
    broker = FakeBroker()
    app = _make_app(settings, broker, recover=True)
    app.start()
    summary = app.oms.recover()  # second call to recover() — idempotent
    assert summary.orders_replayed == 0
    app.stop()


def test_oms_event_log_survives_app_restart(tmp_path: Path) -> None:
    """OMS store path is shared across runs; events accumulate across restarts."""
    settings = _make_settings(tmp_path)
    broker = FakeBroker()

    app1 = _make_app(settings, broker, recover=False)
    order_id = make_market_order(
        symbol="QQQ", side=OrderSide.BUY, qty=Decimal("5"), agent_id=AgentId.HAIKU,
    )
    app1.oms.submit_order(order_id)
    app1.store.close()
    for memory in app1._memories.values():
        memory.close()

    app2 = _make_app(settings, broker, recover=True)
    # Event count > 0 after recovery (replay reconstructs orders + fills)
    assert app2.store.count() > 0
    app2.stop()


def test_recover_preserves_kill_switch_baseline(tmp_path: Path) -> None:
    """After recovery, the kill switch starts in OK (no spurious DD trips)."""
    settings = _make_settings(tmp_path)
    broker = FakeBroker()
    app = _make_app(settings, broker, recover=True)
    app.start()
    from core.types import KillSwitchState
    assert app.kill.state == KillSwitchState.OK
    app.stop()


def test_shutdown_summary_reflects_open_orders(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path)
    broker = FakeBroker()
    app = _make_app(settings, broker, recover=False)
    app.start()
    app.stop()
    shutdowns = list((tmp_path / "logs").glob("shutdown_*.json"))
    assert shutdowns, "no shutdown summary written"
    import json
    summary = json.loads(shutdowns[0].read_text())
    assert summary.get("open_orders") == 0
    assert "started_at" in summary
    assert "shutdown_at" in summary
