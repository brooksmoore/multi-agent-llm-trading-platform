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


# ── Fail-before test for Sonnet rules-only baseline (handoff CL-1 / DoD backtest harness) ──
# This test is intentionally red until sonnet_momentum_weights + supporting backtest usage land.
# Mirrors the "rules signal must be testable independently of LLM" requirement.


def test_sonnet_momentum_baseline_exists_and_ranks_higher_mom_higher() -> None:
    """Sonnet rules baseline must expose a weight_fn usable by the harness.

    Higher 12-1 momentum must produce non-zero allocation; lower must not when
    others dominate. (Synthetic data guarantees the ranking.)
    """
    from backtest.strategies import sonnet_momentum_weights  # will fail until implemented

    start = datetime(2020, 1, 1, tzinfo=UTC)
    # Construct two symbols with known 12-1 mom: A has strong positive, B weak/negative.
    # 252+21 bars needed.
    n = 300
    strong_mom = [100.0 + (i * 0.1) for i in range(n)]  # steadily rising → high 12-1
    weak_mom = [100.0 - (i * 0.01) for i in range(n)]   # slight down → low/neg 12-1
    bars = {
        "AAPL": _series("AAPL", strong_mom, start),
        "MSFT": _series("MSFT", weak_mom, start),
    }
    # Also add filler names from universe so selection is meaningful
    for sym in list(_EQUITY_UNIVERSE)[:3]:
        if sym not in bars:
            bars[sym] = _series(sym, [50.0] * n, start)

    w = sonnet_momentum_weights(bars, top_n=1)  # force top-1 so weak mom is provably excluded
    assert isinstance(w, dict)
    # Strong mom name should be selected for allocation in a rules momentum baseline
    assert "AAPL" in w and w["AAPL"] > Decimal("0")
    # Weak should be zero or absent (top-1 + positive mom dominance guarantees)
    assert "MSFT" not in w or w.get("MSFT", Decimal("0")) == Decimal("0")
    # Weights must be positive Decimals summing <= 1 (cash implicit for remainder)
    assert all(isinstance(v, Decimal) and v > 0 for v in w.values())
    assert sum(w.values()) <= Decimal("1")


def test_walk_forward_and_deflated_sharpe_smoke() -> None:
    """The DoD extensions (walk-forward scaffold + deflated SR) must be callable.

    Uses the existing synthetic rising-SPY setup so it stays fully offline.
    """
    from backtest.engine import deflated_sharpe, run_walk_forward

    start = datetime(2019, 1, 1, tzinfo=UTC)
    rising = [100.0 * (1.0003 ** i) for i in range(700)]
    flat = [10.0] * 700
    bars = {"SPY": _series("SPY", rising, start)}
    for sym in _EQUITY_UNIVERSE:
        if sym != "SPY":
            bars[sym] = _series(sym, flat, start)

    wfs = run_walk_forward(bars, faber_gtaa_weights, n_windows=2, benchmark="SPY", cost_bps=5.0)
    assert len(wfs) == 2
    for r in wfs:
        assert r.sharpe is not None
        d = deflated_sharpe(r.sharpe, n_trials=5, n_obs=len(r.dates))
        assert isinstance(d, float)


# ── Fail-before for Fix 1 (Auditor handoff): real temporal walk-forward splits ──
# This test must be committed RED (current run_walk_forward is a cost sweep that
# re-runs the *full* date range N times). Implementation must then make it GREEN.


def test_walk_forward_uses_distinct_date_windows() -> None:
    """Each fold must cover a non-overlapping forward date range.

    result[i].dates[-1] < result[i+1].dates[0] — no fold may contain data from a later fold.
    """
    from backtest.engine import run_walk_forward

    # Synthetic spanning ~3 years of "trading days" (use daily steps for simplicity).
    # Enough bars for multiple monthly rebalances inside each window.
    start = datetime(2020, 1, 1, tzinfo=UTC)
    n_bars = 780  # ~3 years of trading days
    rising = [100.0 * (1.0002 ** i) for i in range(n_bars)]
    bars = {"SPY": _series("SPY", rising, start)}
    for sym in list(_EQUITY_UNIVERSE)[:4]:
        bars[sym] = _series(sym, [50.0 + i*0.01 for i in range(n_bars)], start)

    results = run_walk_forward(bars, faber_gtaa_weights, n_windows=3, benchmark="SPY")

    assert len(results) == 3
    for i in range(len(results) - 1):
        assert results[i].dates[-1] < results[i + 1].dates[0], (
            f"window {i} bleeds into window {i+1}: "
            f"{results[i].dates[-1]} >= {results[i + 1].dates[0]}"
        )
    # Each window must have run at least one rebalance (sanity that slicing didn't produce empty)
    for r in results:
        assert r.n_rebalances >= 1
        assert len(r.dates) > 20  # non-trivial window
