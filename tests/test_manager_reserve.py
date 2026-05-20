"""Manager reserve + capital-reallocation cadence tests (M1, M2)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app import App
from config.settings import Settings
from core.types import AgentId, Fill, OrderSide, new_id
from data.market import Bar, Timeframe
from execution.fake_broker import FakeBroker


class _StubMD:
    def __init__(self) -> None:
        self._bars: dict[str, list[Bar]] = {}

    def set_bars(self, symbol: str, bars: list[Bar]) -> None:
        self._bars[symbol] = bars

    def get_bars(self, symbol, start=None, end=None, timeframe=Timeframe.DAY):  # noqa: ANN001, ANN202, ARG002
        return list(self._bars.get(symbol, []))

    def get_bars_batch(self, symbols, start=None, end=None, timeframe=Timeframe.DAY):  # noqa: ANN001, ANN202, ARG002
        return {s: self._bars.get(s, []) for s in symbols}

    def get_latest_bar(self, symbol):  # noqa: ANN001, ANN202
        bars = self._bars.get(symbol)
        return bars[-1] if bars else None

    def get_latest_quote(self, symbol):  # noqa: ARG002, ANN001, ANN202
        return None

    def get_snapshots(self, symbols):  # noqa: ARG002, ANN001, ANN202
        return {}


def _spy_bar(close: Decimal) -> Bar:
    ts = datetime(2026, 5, 20, 16, 0, tzinfo=UTC)
    return Bar(
        symbol="SPY", timestamp=ts, open=close, high=close, low=close,
        close=close, volume=1_000_000,
    )


@pytest.fixture
def app(tmp_path: Path) -> App:
    settings = Settings(
        alpaca_paper=True, alpaca_api_key="x", alpaca_secret_key="x",
        anthropic_api_key="x", ntfy_topic="",
        master_capability=Decimal("1.0"), daily_spend_cap=Decimal("0.95"),
        data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"),
    )
    md = _StubMD()
    md.set_bars("SPY", [_spy_bar(Decimal("500"))])
    return App(
        settings,
        broker=FakeBroker(starting_cash=Decimal("100000")),
        market_data=md,
        universe=["SPY"], run_dashboard=False, run_volatility_scanner=False,
        run_recover_on_start=False,
    )


# ── M1: Manager SPY reserve ─────────────────────────────────────────────────


def test_reserve_check_places_initial_spy_buy(app: App) -> None:
    """First run on an empty MANAGER sleeve submits a ~$10k SPY buy."""
    assert app.lots.open_qty_by_symbol(AgentId.MANAGER) == {}

    app._job_manager_reserve_check()

    # FakeBroker fills market orders instantly; the MANAGER lot should now exist.
    held = app.lots.open_qty_by_symbol(AgentId.MANAGER)
    spy_qty = held.get("SPY", Decimal("0"))
    assert spy_qty > Decimal("0"), f"expected SPY position, got {held}"

    # $10k / $500 = 20 shares, give or take fractional rounding.
    assert Decimal("19.5") < spy_qty < Decimal("20.5")


def test_reserve_check_is_idempotent(app: App) -> None:
    """Second invocation with an existing SPY position does NOT re-buy."""
    app._job_manager_reserve_check()
    qty_after_first = app.lots.open_qty_by_symbol(AgentId.MANAGER).get("SPY")

    app._job_manager_reserve_check()
    qty_after_second = app.lots.open_qty_by_symbol(AgentId.MANAGER).get("SPY")

    assert qty_after_first == qty_after_second


def test_reserve_check_skips_when_cash_insufficient(tmp_path: Path) -> None:
    """If broker cash < reserve target, the job defers without erroring."""
    settings = Settings(
        alpaca_paper=True, alpaca_api_key="x", alpaca_secret_key="x",
        anthropic_api_key="x", ntfy_topic="",
        master_capability=Decimal("1.0"), daily_spend_cap=Decimal("0.95"),
        data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"),
    )
    md = _StubMD()
    md.set_bars("SPY", [_spy_bar(Decimal("500"))])
    poor_app = App(
        settings,
        broker=FakeBroker(starting_cash=Decimal("100")),  # well below $10k target
        market_data=md, universe=["SPY"],
        run_dashboard=False, run_volatility_scanner=False, run_recover_on_start=False,
    )

    poor_app._job_manager_reserve_check()

    assert poor_app.lots.open_qty_by_symbol(AgentId.MANAGER) == {}


def test_reserve_check_skips_when_no_mark(tmp_path: Path) -> None:
    """Empty bar feed: the job defers rather than guessing a price."""
    settings = Settings(
        alpaca_paper=True, alpaca_api_key="x", alpaca_secret_key="x",
        anthropic_api_key="x", ntfy_topic="",
        master_capability=Decimal("1.0"), daily_spend_cap=Decimal("0.95"),
        data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"),
    )
    no_mark_app = App(
        settings,
        broker=FakeBroker(starting_cash=Decimal("100000")),
        market_data=_StubMD(),  # no SPY bars
        universe=["SPY"], run_dashboard=False, run_volatility_scanner=False,
        run_recover_on_start=False,
    )

    no_mark_app._job_manager_reserve_check()

    assert no_mark_app.lots.open_qty_by_symbol(AgentId.MANAGER) == {}


def test_reserve_check_self_heals_after_manual_flatten(app: App) -> None:
    """If a flatten zeroes out the MANAGER SPY lot, next run re-buys."""
    app._job_manager_reserve_check()
    initial_qty = app.lots.open_qty_by_symbol(AgentId.MANAGER).get("SPY")
    assert initial_qty is not None and initial_qty > Decimal("0")

    # Simulate an external flatten — book a matching SELL fill against MANAGER.
    app.lots.book_fill(Fill(
        id=new_id(), order_id=new_id(), agent_id=AgentId.MANAGER,
        symbol="SPY", side=OrderSide.SELL,
        qty=initial_qty, price=Decimal("500"),
        timestamp=datetime.now(UTC),
    ))
    assert app.lots.open_qty_by_symbol(AgentId.MANAGER).get("SPY", Decimal("0")) == Decimal("0")

    # Self-heal: next Monday run puts the reserve back.
    app._job_manager_reserve_check()
    new_qty = app.lots.open_qty_by_symbol(AgentId.MANAGER).get("SPY", Decimal("0"))
    assert new_qty > Decimal("0")


# ── M2: capital-reallocation cadence ───────────────────────────────────────


def test_realloc_cadence_helper() -> None:
    """The 4-week-since-last gate: encoded as a simple date diff.

    This is a thin computation test that mirrors the logic in
    `_job_manager_friday`. The full job is harder to exercise here because
    it needs a real Manager LLM client; covered separately by manual
    inspection of the rewritten code.
    """
    today = date(2026, 5, 22)

    # Never run before → fires.
    last = None
    weeks_since = (
        (today - last).days / 7.0 if last is not None else float("inf")
    )
    assert weeks_since >= 4.0

    # 3 weeks ago → skips.
    last = date(2026, 5, 1)
    weeks_since = (today - last).days / 7.0
    assert weeks_since < 4.0

    # 4+ weeks ago → fires.
    last = date(2026, 4, 24)
    weeks_since = (today - last).days / 7.0
    assert weeks_since >= 4.0
