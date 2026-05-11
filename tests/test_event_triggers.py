"""Event-driven trigger tests (T2.5).

Covers:
- _on_news_high_impact: triggers an off-schedule Opus deep dive on a
  held name with ISO-week rate limiting.
- _scan_volatility_once: publishes PositionIntradayShockEvent on >5%
  intraday move on a held name.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app import App
from config.settings import Settings
from core.events import (
    NewsHighImpactScoredEvent,
    PositionIntradayShockEvent,
)
from core.types import AgentId, Fill, OrderSide, new_id
from data.market import Bar, Timeframe
from execution.broker import AssetClass, BrokerPosition
from execution.fake_broker import FakeBroker


@pytest.fixture
def app(tmp_path: Path) -> App:
    class _StubMD:
        def __init__(self) -> None:
            self._bars: dict[str, list[Bar]] = {}

        def set_bars(self, symbol: str, bars: list[Bar]) -> None:
            self._bars[symbol] = bars

        def get_bars(  # noqa: ANN202
            self, symbol, start=None, end=None, timeframe=Timeframe.DAY,  # noqa: ARG002, ANN001
        ):
            return list(self._bars.get(symbol, []))

        def get_bars_batch(  # noqa: ANN202
            self, symbols, start=None, end=None, timeframe=Timeframe.DAY,  # noqa: ARG002, ANN001
        ):
            return {s: self._bars.get(s, []) for s in symbols}

        def get_latest_bar(self, symbol):  # noqa: ANN001, ANN202
            bars = self._bars.get(symbol)
            return bars[-1] if bars else None

        def get_latest_quote(self, symbol):  # noqa: ARG002, ANN001, ANN202
            return None

        def get_snapshots(self, symbols):  # noqa: ARG002, ANN001, ANN202
            return {}

    settings = Settings(
        alpaca_paper=True, alpaca_api_key="x", alpaca_secret_key="x",
        anthropic_api_key="x", ntfy_topic="",
        master_capability=Decimal("1.0"), daily_spend_cap=Decimal("0.95"),
        data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"),
    )
    return App(
        settings, broker=FakeBroker(), market_data=_StubMD(),
        universe=["SPY"], run_dashboard=False, run_volatility_scanner=False,
        run_recover_on_start=False,
    )


def _make_bar(symbol: str, close: Decimal, day_offset: int = 0) -> Bar:
    ts = datetime(2026, 5, 11, 16, 0, tzinfo=UTC) + timedelta(days=day_offset)
    return Bar(
        symbol=symbol, timestamp=ts, open=close, high=close, low=close,
        close=close, volume=1_000_000,
    )


# ── News-driven off-schedule deep dive ────────────────────────────────────────


def test_news_event_triggers_deep_dive_on_held_opus_name(app: App) -> None:
    """High-impact news on an Opus-held name fires _opus_run_deep_dive."""
    # Seed an Opus holding via the lot ledger
    app.lots.book_fill(Fill(
        id=new_id(), order_id=new_id(), agent_id=AgentId.OPUS,
        symbol="TSM", side=OrderSide.BUY,
        qty=Decimal("5"), price=Decimal("200"),
        timestamp=datetime.now(UTC),
    ))
    app._opus_run_deep_dive = MagicMock()

    app.bus.publish(NewsHighImpactScoredEvent(
        symbol="TSM", impact=4, headline="TSM news", published_at=datetime.now(UTC),
    ))

    app._opus_run_deep_dive.assert_called_once_with("TSM")


def test_news_event_skips_unheld_name(app: App) -> None:
    """News on a name Opus doesn't hold does NOT trigger a deep dive."""
    app._opus_run_deep_dive = MagicMock()

    app.bus.publish(NewsHighImpactScoredEvent(
        symbol="UNHELD", impact=5, headline="huge news",
        published_at=datetime.now(UTC),
    ))

    app._opus_run_deep_dive.assert_not_called()


def test_news_event_rate_limited_to_one_per_iso_week(app: App) -> None:
    """Second high-impact event in same ISO week is suppressed."""
    app.lots.book_fill(Fill(
        id=new_id(), order_id=new_id(), agent_id=AgentId.OPUS,
        symbol="TSM", side=OrderSide.BUY, qty=Decimal("5"),
        price=Decimal("200"), timestamp=datetime.now(UTC),
    ))
    app.lots.book_fill(Fill(
        id=new_id(), order_id=new_id(), agent_id=AgentId.OPUS,
        symbol="ASML", side=OrderSide.BUY, qty=Decimal("3"),
        price=Decimal("700"), timestamp=datetime.now(UTC),
    ))
    app._opus_run_deep_dive = MagicMock()

    # First event: fires the dive
    app.bus.publish(NewsHighImpactScoredEvent(
        symbol="TSM", impact=4, headline="TSM news",
        published_at=datetime.now(UTC),
    ))
    # Second event in the same ISO week: should be rate-limited
    app.bus.publish(NewsHighImpactScoredEvent(
        symbol="ASML", impact=5, headline="ASML news",
        published_at=datetime.now(UTC),
    ))

    assert app._opus_run_deep_dive.call_count == 1
    # The second call DIDN'T happen because the first marked the week as used


def test_news_event_failed_dive_does_not_burn_quota(app: App) -> None:
    """If _opus_run_deep_dive raises, the rate-limit slot is NOT marked used."""
    app.lots.book_fill(Fill(
        id=new_id(), order_id=new_id(), agent_id=AgentId.OPUS,
        symbol="TSM", side=OrderSide.BUY, qty=Decimal("5"),
        price=Decimal("200"), timestamp=datetime.now(UTC),
    ))
    app._opus_run_deep_dive = MagicMock(side_effect=RuntimeError("network"))

    # First publish triggers the failing dive.
    app.bus.publish(NewsHighImpactScoredEvent(
        symbol="TSM", impact=4, headline="x", published_at=datetime.now(UTC),
    ))

    # Recovery: quota not used; second publish on a working dive succeeds.
    app._opus_run_deep_dive = MagicMock()
    app.bus.publish(NewsHighImpactScoredEvent(
        symbol="TSM", impact=4, headline="x2", published_at=datetime.now(UTC),
    ))

    app._opus_run_deep_dive.assert_called_once()


# ── PositionIntradayShockEvent from _scan_volatility_once ─────────────────────


def test_volatility_scan_publishes_shock_on_held_name(app: App) -> None:
    """A >5% move on a held name fires PositionIntradayShockEvent."""
    today = datetime.now(UTC).date()
    # Seed FakeBroker with one held position
    app.broker._positions = {  # type: ignore[attr-defined]
        "SPY": BrokerPosition(
            symbol="SPY", qty=Decimal("10"),
            avg_entry_price=Decimal("400"),
            current_price=Decimal("440"),
            asset_class=AssetClass.ETF,
        ),
    }
    # Seed market data: prev_close=400, current=440 → +10% shock
    app.market_data.set_bars("SPY", [  # type: ignore[attr-defined]
        _make_bar("SPY", Decimal("400"), day_offset=-1),
        _make_bar("SPY", Decimal("440"), day_offset=0),
    ])

    received: list[PositionIntradayShockEvent] = []
    app.bus.subscribe("position.intraday_shock", lambda e: received.append(e))  # type: ignore[arg-type]

    app._scan_volatility_once(today)

    assert len(received) == 1
    assert received[0].symbol == "SPY"
    assert received[0].shock_pct == Decimal("0.1")


def test_volatility_scan_ignores_sub_threshold_move(app: App) -> None:
    """A <5% move does NOT fire the shock event."""
    today = datetime.now(UTC).date()
    app.broker._positions = {  # type: ignore[attr-defined]
        "SPY": BrokerPosition(
            symbol="SPY", qty=Decimal("10"),
            avg_entry_price=Decimal("400"),
            current_price=Decimal("408"),
            asset_class=AssetClass.ETF,
        ),
    }
    app.market_data.set_bars("SPY", [  # type: ignore[attr-defined]
        _make_bar("SPY", Decimal("400"), day_offset=-1),
        _make_bar("SPY", Decimal("408"), day_offset=0),  # +2%
    ])

    received: list[PositionIntradayShockEvent] = []
    app.bus.subscribe("position.intraday_shock", lambda e: received.append(e))  # type: ignore[arg-type]

    app._scan_volatility_once(today)

    assert received == []


def test_volatility_scan_publishes_negative_shock_too(app: App) -> None:
    """A -7% move is also a shock (absolute value semantics)."""
    today = datetime.now(UTC).date()
    app.broker._positions = {  # type: ignore[attr-defined]
        "SPY": BrokerPosition(
            symbol="SPY", qty=Decimal("10"),
            avg_entry_price=Decimal("400"),
            current_price=Decimal("372"),
            asset_class=AssetClass.ETF,
        ),
    }
    app.market_data.set_bars("SPY", [  # type: ignore[attr-defined]
        _make_bar("SPY", Decimal("400"), day_offset=-1),
        _make_bar("SPY", Decimal("372"), day_offset=0),  # -7%
    ])

    received: list[PositionIntradayShockEvent] = []
    app.bus.subscribe("position.intraday_shock", lambda e: received.append(e))  # type: ignore[arg-type]

    app._scan_volatility_once(today)

    assert len(received) == 1
    assert received[0].shock_pct < Decimal("0")
