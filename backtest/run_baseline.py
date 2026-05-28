"""Run the rules-only Faber GTAA baseline against SPY, net of costs.

    uv run python -m backtest.run_baseline                 # ~8y, equities only
    uv run python -m backtest.run_baseline --years 5 --crypto
    uv run python -m backtest.run_baseline --cost-bps 10   # stress costs

This is the control the blueprint's honest assessment asks for: if the
deterministic baseline already beats SPY, the LLM has to beat the baseline (not
just SPY) to justify its cost. If the baseline lags SPY, the strategy family —
not the LLM — is the problem.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from functools import partial

from agents.haiku_agent import _CRYPTO_UNIVERSE, _EQUITY_UNIVERSE
from backtest.engine import run_backtest
from backtest.strategies import faber_gtaa_weights
from config.settings import Settings
from data.market import MarketData, YFinanceMarketData


def _build_market_data(settings: Settings) -> MarketData:
    # Deliberately NOT wrapped in CachedMarketData: the bar cache exists for the
    # live hot path and only retains a rolling recent window, which silently
    # truncates a multi-year backtest. Pull the full range straight from the
    # source instead.
    return YFinanceMarketData(
        alpaca_api_key=settings.alpaca_api_key,
        alpaca_secret_key=settings.alpaca_secret_key,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--years", type=float, default=8.0, help="lookback window")
    ap.add_argument("--cost-bps", type=float, default=5.0, help="one-way turnover cost")
    ap.add_argument("--crypto", action="store_true", help="include crypto sleeve")
    args = ap.parse_args()

    settings = Settings()
    md = _build_market_data(settings)

    end = datetime.now(UTC)
    start = end - timedelta(days=int(args.years * 365.25))
    universe = list(_EQUITY_UNIVERSE)
    if args.crypto:
        universe += list(_CRYPTO_UNIVERSE)

    print(f"fetching {len(universe)} symbols, {start.date()} → {end.date()} ...")
    bars = md.get_bars_batch(universe, start, end)
    have = {s: len(b) for s, b in bars.items()}
    print("bars fetched:", have)

    result = run_backtest(
        bars,
        partial(faber_gtaa_weights, include_crypto=args.crypto),
        benchmark="SPY",
        cost_bps=args.cost_bps,
    )
    print("\n=== Rules-only Faber GTAA baseline vs SPY ===")
    print(result.summary())


if __name__ == "__main__":
    main()
