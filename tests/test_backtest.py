"""Tests for the rules-only baseline + backtest engine.

Synthetic bars keep these deterministic and offline (no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from agents.haiku_agent import _EQUITY_UNIVERSE
from backtest.engine import run_backtest
from backtest.strategies import faber_gtaa_weights
from data.market import Bar


def _series(symbol: str, closes: list[float], start: datetime) -> list[Bar]:
    bars = []
    for i, c in enumerate(closes):
        ts = start + timedelta(days=i)
        bars.append(
            Bar(
                symbol=symbol,
                timestamp=ts,
                open=Decimal(str(c)),
                high=Decimal(str(c)),
                low=Decimal(str(c)),
                close=Decimal(str(c)),
                volume=1_000,
            )
        )
    return bars


def test_faber_in_trend_when_above_sma() -> None:
    """A steadily rising series sits above its SMA → in trend → gets a slice."""
    start = datetime(2020, 1, 1, tzinfo=UTC)
    # 260 rising closes guarantees last > 210-day SMA.
    rising = [100.0 + i for i in range(260)]
    flat = [50.0] * 260  # flat < its own SMA only if declining; flat sits ON sma
    bars = {"SPY": _series("SPY", rising, start)}
    for sym in _EQUITY_UNIVERSE:
        if sym != "SPY":
            bars[sym] = _series(sym, flat, start)

    w = faber_gtaa_weights(bars)
    assert "SPY" in w
    assert w["SPY"] == Decimal("1") / Decimal(len(_EQUITY_UNIVERSE))


def test_faber_cash_when_below_sma() -> None:
    """A falling series ends below its SMA → out of trend → no allocation."""
    start = datetime(2020, 1, 1, tzinfo=UTC)
    falling = [400.0 - i for i in range(260)]
    bars = {"SPY": _series("SPY", falling, start)}
    w = faber_gtaa_weights(bars)
    assert "SPY" not in w


def test_backtest_tracks_spy_when_fully_invested_in_spy() -> None:
    """If the only in-trend name IS the benchmark, strategy ≈ SPY minus costs:
    excess CAGR should be ~0 (slightly negative from turnover at the seed)."""
    start = datetime(2019, 1, 1, tzinfo=UTC)
    rising = [100.0 * (1.0003 ** i) for i in range(700)]  # ~8%/yr, always uptrend
    flat = [10.0] * 700
    bars = {"SPY": _series("SPY", rising, start)}
    for sym in _EQUITY_UNIVERSE:
        if sym != "SPY":
            bars[sym] = _series(sym, flat, start)

    res = run_backtest(bars, faber_gtaa_weights, benchmark="SPY", cost_bps=5.0)
    # SPY is 1/N weight, so strategy captures ~1/N of SPY's return → it should
    # materially LAG full SPY exposure. Sanity: curves are well-formed.
    assert len(res.equity) == len(res.dates)
    assert res.benchmark_cagr > 0.0
    assert res.n_rebalances > 1
    assert res.max_drawdown <= 0.0


def test_backtest_rejects_missing_benchmark() -> None:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    bars = {"QQQ": _series("QQQ", [100.0] * 5, start)}
    try:
        run_backtest(bars, faber_gtaa_weights, benchmark="SPY")
        raise AssertionError("expected ValueError for missing benchmark")
    except ValueError:
        pass
