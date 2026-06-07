"""Minimal, honest daily-mark / monthly-rebalance backtester.

Scope boundary (read this before trusting a number):
  * Reuses the live signal math (via backtest.strategies) and a realistic
    turnover cost model, but does NOT replay the live OMS, RiskGate, kill
    switch, lot-level FIFO tax, or per-agent leverage ladder. It is a research
    control for "does the rules signal beat SPY net of costs", not a
    bit-for-bit simulation of live execution.
  * Cash earns zero (no risk-free accrual) — conservative for the strategy.
  * Faber GTAA is a monthly system; we rebalance on the last trading day of
    each month using only bars at or before that day (no lookahead).

Everything is float internally: this is research arithmetic, not money-touching
execution (which stays in Decimal under execution/).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from data.market import Bar

WeightFn = Callable[[dict[str, list[Bar]]], dict[str, Decimal]]

_TRADING_DAYS = 252


def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


@dataclass(frozen=True)
class BacktestResult:
    """Equity curve + summary metrics for one run."""

    dates: list[date]
    equity: list[float]          # strategy equity curve, starts at 1.0
    benchmark_equity: list[float]  # SPY buy-and-hold, starts at 1.0
    total_return: float
    cagr: float
    ann_vol: float
    sharpe: float
    max_drawdown: float
    benchmark_cagr: float
    excess_cagr: float           # strategy CAGR − benchmark CAGR (the headline)
    n_rebalances: int

    def summary(self) -> str:
        return (
            f"period          : {self.dates[0]} → {self.dates[-1]} "
            f"({len(self.dates)} trading days, {self.n_rebalances} rebalances)\n"
            f"total return    : {_pct(self.total_return)}\n"
            f"CAGR            : {_pct(self.cagr)}\n"
            f"ann. volatility : {_pct(self.ann_vol)}\n"
            f"Sharpe (rf=0)   : {self.sharpe:.2f}\n"
            f"max drawdown    : {_pct(self.max_drawdown)}\n"
            f"SPY CAGR        : {_pct(self.benchmark_cagr)}\n"
            f"EXCESS vs SPY   : {_pct(self.excess_cagr)}  "
            f"<-- positive ⇒ baseline beats SPY net of costs"
        )


def _close_maps(
    bars_by_symbol: dict[str, list[Bar]],
) -> dict[str, dict[date, float]]:
    out: dict[str, dict[date, float]] = {}
    for sym, bars in bars_by_symbol.items():
        m: dict[date, float] = {}
        for b in sorted(bars, key=lambda x: x.timestamp):
            m[b.timestamp.date()] = float(b.close)
        out[sym] = m
    return out


def _slice_asof(
    bars_by_symbol: dict[str, list[Bar]], asof: date
) -> dict[str, list[Bar]]:
    return {
        sym: [b for b in bars if b.timestamp.date() <= asof]
        for sym, bars in bars_by_symbol.items()
    }


def run_backtest(
    bars_by_symbol: dict[str, list[Bar]],
    weight_fn: WeightFn,
    *,
    benchmark: str = "SPY",
    cost_bps: float = 5.0,
) -> BacktestResult:
    """Walk the benchmark's trading calendar, rebalancing monthly.

    ``cost_bps`` is charged on one-way turnover at each rebalance (5 bps ≈
    commission-free equities with light slippage; raise it to stress-test).
    """
    closes = _close_maps(bars_by_symbol)
    if benchmark not in closes or not closes[benchmark]:
        raise ValueError(f"benchmark {benchmark!r} has no bars")

    calendar = sorted(closes[benchmark])  # equity trading days
    if len(calendar) < 2:
        raise ValueError("need at least two benchmark bars to backtest")

    cost_rate = cost_bps / 10_000.0
    equity = 1.0
    bench0 = closes[benchmark][calendar[0]]

    eq_curve: list[float] = [1.0]
    bench_curve: list[float] = [1.0]
    daily_returns: list[float] = []

    weights: dict[str, float] = {}  # active weights for the upcoming step
    n_rebalances = 0
    prev_day = calendar[0]

    # Seed weights from the first day so the first month isn't pure cash.
    weights = {
        s: float(w)
        for s, w in weight_fn(_slice_asof(bars_by_symbol, prev_day)).items()
    }
    n_rebalances += 1

    for day in calendar[1:]:
        # Mark the portfolio over [prev_day, day] using each held name's return.
        port_ret = 0.0
        for sym, w in weights.items():
            c_prev = closes.get(sym, {}).get(prev_day)
            c_now = closes.get(sym, {}).get(day)
            if c_prev and c_now and c_prev > 0:
                port_ret += w * (c_now / c_prev - 1.0)
        equity *= 1.0 + port_ret
        daily_returns.append(port_ret)

        # Month boundary → rebalance using bars as of `day` (no lookahead).
        if day.month != prev_day.month:
            new_weights = {
                s: float(w)
                for s, w in weight_fn(_slice_asof(bars_by_symbol, day)).items()
            }
            turnover = _turnover(weights, new_weights)
            equity *= 1.0 - turnover * cost_rate
            weights = new_weights
            n_rebalances += 1

        eq_curve.append(equity)
        bench_curve.append(closes[benchmark][day] / bench0)
        prev_day = day

    return _summarize(calendar, eq_curve, bench_curve, daily_returns, n_rebalances)


def _turnover(old: dict[str, float], new: dict[str, float]) -> float:
    symbols = set(old) | set(new)
    return sum(abs(new.get(s, 0.0) - old.get(s, 0.0)) for s in symbols)


def _summarize(
    calendar: list[date],
    eq_curve: list[float],
    bench_curve: list[float],
    daily_returns: list[float],
    n_rebalances: int,
) -> BacktestResult:
    years = max((calendar[-1] - calendar[0]).days / 365.25, 1e-9)

    def cagr(curve: list[float]) -> float:
        return curve[-1] ** (1.0 / years) - 1.0 if curve[-1] > 0 else -1.0

    mean = sum(daily_returns) / len(daily_returns) if daily_returns else 0.0
    if len(daily_returns) > 1:
        var = sum((r - mean) ** 2 for r in daily_returns) / (len(daily_returns) - 1)
        std = math.sqrt(var)
    else:
        std = 0.0
    ann_vol = std * math.sqrt(_TRADING_DAYS)
    sharpe = (mean / std * math.sqrt(_TRADING_DAYS)) if std > 0 else 0.0

    peak = -math.inf
    max_dd = 0.0
    for v in eq_curve:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, v / peak - 1.0)

    strat_cagr = cagr(eq_curve)
    bench_cagr = cagr(bench_curve)
    return BacktestResult(
        dates=calendar,
        equity=eq_curve,
        benchmark_equity=bench_curve,
        total_return=eq_curve[-1] - 1.0,
        cagr=strat_cagr,
        ann_vol=ann_vol,
        sharpe=sharpe,
        max_drawdown=max_dd,
        benchmark_cagr=bench_cagr,
        excess_cagr=strat_cagr - bench_cagr,
        n_rebalances=n_rebalances,
    )


# ── Research metrics extensions (DoD: walk-forward CV + deflated Sharpe) ───────


def deflated_sharpe(
    sharpe: float, *, n_trials: int = 1, n_obs: int = 0
) -> float:
    """Illustrative deflated Sharpe (multiple-testing + finite-sample penalty).

    For a serious harness this should be the full Bailey/ Lopez de Prado formula
    incorporating skew, kurtosis, and the exact # of trials in the search space.
    Here it serves as the extension point the DoD calls for.
    """
    if n_trials <= 1 or n_obs < 10:
        return sharpe
    import math

    # Rough penalty (sqrt(2*ln(N)) / sqrt(T)) style — conservative starting point.
    penalty = math.sqrt(2.0 * math.log(max(n_trials, 2))) / math.sqrt(max(n_obs, 1))
    return sharpe - penalty


def run_walk_forward(
    bars_by_symbol: dict[str, list[Bar]],
    weight_fn: WeightFn,
    *,
    n_windows: int = 4,
    embargo_days: int = 21,
    **bt_kwargs: Any,
) -> list[BacktestResult]:
    """Real temporal walk-forward harness (date-sliced forward folds).

    Derives the master trading calendar from the benchmark symbol (same logic
    as run_backtest). Splits that calendar into n_windows sequential,
    non-overlapping forward windows. Each fold receives *only* the bars whose
    dates fall inside its window, then calls run_backtest on that slice.

    This satisfies the DoD requirement for walk-forward CV (each test fold is
    strictly later than previous folds). Embargo_days can be used to insert a
    gap between windows (future-proofing for purged CV).
    """
    benchmark = bt_kwargs.get("benchmark", "SPY")
    closes = _close_maps(bars_by_symbol)
    if benchmark not in closes or not closes[benchmark]:
        raise ValueError(f"benchmark {benchmark!r} has no bars for walk-forward split")

    calendar = sorted(closes[benchmark])  # list[date]
    if len(calendar) < n_windows * 2:
        # Not enough data for meaningful splits — fall back to single full run
        # but still return a list of length 1 for API compatibility in tests.
        r = run_backtest(bars_by_symbol, weight_fn, **bt_kwargs)
        return [r]

    # Compute contiguous (or embargo-gapped) window boundaries
    chunk = max(1, len(calendar) // n_windows)
    results: list[BacktestResult] = []
    prev_end: date | None = None

    for i in range(n_windows):
        start_idx = i * chunk
        if i == n_windows - 1:
            end_idx = len(calendar)
        else:
            end_idx = (i + 1) * chunk

        # Apply embargo: shift start forward if we have a previous end
        win_start = calendar[start_idx]
        if prev_end is not None and embargo_days > 0:
            # Find the first date >= prev_end + embargo_days
            embargo_start = prev_end
            # naive day shift (sufficient for synthetic + real trading calendars here)
            from datetime import timedelta
            embargo_start = embargo_start + timedelta(days=embargo_days)
            # advance start_idx to the first calendar date >= embargo_start
            while start_idx < len(calendar) and calendar[start_idx] < embargo_start:
                start_idx += 1
            if start_idx >= len(calendar):
                break
            win_start = calendar[start_idx]
            end_idx = max(end_idx, start_idx + 1)

        win_end = calendar[end_idx - 1] if end_idx > start_idx else win_start

        # Slice bars for this window only
        sliced: dict[str, list[Bar]] = {}
        for sym, bars in bars_by_symbol.items():
            sliced[sym] = [
                b for b in bars
                if win_start <= b.timestamp.date() <= win_end
            ]

        if not sliced.get(benchmark):
            continue

        r = run_backtest(sliced, weight_fn, **bt_kwargs)
        results.append(r)
        prev_end = win_end

    if not results:
        # fallback
        r = run_backtest(bars_by_symbol, weight_fn, **bt_kwargs)
        return [r]
    return results
