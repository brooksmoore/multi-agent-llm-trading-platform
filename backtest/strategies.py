"""Deterministic, LLM-free strategies — the rules-only baseline.

The Faber GTAA baseline mirrors the *exact* signal the live Haiku sleeve
computes (`agents.haiku_agent._sma` / `_momentum` and the same universe and
period constants). Importing those helpers rather than re-deriving them is
deliberate: it guarantees the baseline and the LLM sleeve see an identical
signal, so any performance difference is attributable to the LLM's judgment on
top of that signal — not to a subtly different rule. That is the whole point of
a control.
"""

from __future__ import annotations

from decimal import Decimal

from agents.haiku_agent import (
    _CRYPTO_UNIVERSE,
    _EQUITY_UNIVERSE,
    _MOMENTUM_DAYS,
    _SMA_PERIOD_CRYPTO,
    _SMA_PERIOD_EQUITY,
    _momentum,
    _sma,
)
from data.market import Bar


def faber_gtaa_weights(
    bars_by_symbol: dict[str, list[Bar]],
    *,
    include_crypto: bool = False,
) -> dict[str, Decimal]:
    """Classic Faber GTAA target weights as of the latest bar in each series.

    Each equity ETF in the universe gets an equal 1/N slice when its last close
    is above its 10-month (210-day) SMA, and cash otherwise — the textbook
    rule. With ``include_crypto`` the three crypto names are added to N and gated
    on the same dual SMA-50 + positive-14d-momentum filter the Haiku sleeve uses.

    Returns a partial weight map (only in-trend names); the unallocated
    remainder is implicit cash. Caller is responsible for as-of correctness:
    pass only bars at or before the rebalance date.
    """
    universe = list(_EQUITY_UNIVERSE)
    if include_crypto:
        universe = universe + list(_CRYPTO_UNIVERSE)
    n = len(universe)
    slice_weight = Decimal("1") / Decimal(n)

    weights: dict[str, Decimal] = {}
    for symbol in universe:
        bars = sorted(bars_by_symbol.get(symbol, []), key=lambda b: b.timestamp)
        closes = [b.close for b in bars]
        if not closes:
            continue
        last = closes[-1]
        if symbol in _CRYPTO_UNIVERSE:
            sma = _sma(closes, _SMA_PERIOD_CRYPTO)
            mom = _momentum(closes, _MOMENTUM_DAYS)
            in_trend = (
                sma is not None and last > sma and mom is not None and mom > Decimal("0")
            )
        else:
            sma = _sma(closes, _SMA_PERIOD_EQUITY)
            in_trend = sma is not None and last > sma
        if in_trend:
            weights[symbol] = slice_weight
    return weights
