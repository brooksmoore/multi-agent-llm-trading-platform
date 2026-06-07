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
from agents.sonnet_agent import _SONNET_TRADABLE
from data.market import Bar

# Sonnet baseline constants (mirrors agents.sonnet_agent for parity)
_SONNET_MOM_LOOKBACK = 252
_SONNET_MOM_SKIP = 21


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


# ── Sonnet rules-only momentum baseline (for backtest harness + DoD "beats rules baseline") ──
# Mirrors agents.sonnet_agent math exactly so any edge is attributable to the LLM layer,
# not a different signal implementation. Pure factor; no LLM, no conviction scalar.


def _price_momentum(closes: list[Decimal], lookback: int, skip: int) -> Decimal | None:
    """Exact mirror of agents.sonnet_agent._price_momentum (12-1, skip last month)."""
    required = lookback + skip
    if len(closes) < required:
        return None
    entry = closes[-(lookback + skip)]
    exit_ = closes[-skip]
    if entry == Decimal("0"):
        return None
    return exit_ / entry - Decimal("1")


def sonnet_momentum_weights(
    bars_by_symbol: dict[str, list[Bar]],
    *,
    top_n: int = 5,
    per_name: Decimal = Decimal("0.08"),
) -> dict[str, Decimal]:
    """Rules-only 12-1 momentum baseline.

    Computes the identical 12-1 momentum the live SonnetAgent uses, selects the
    top-N names with valid history, assigns each `per_name` (remainder implicit cash).
    Used by the backtest harness to establish the pure-factor control that the
    LLM Sonnet must beat on a like-for-like signal.

    No conviction, no LLM output, deterministic.
    """
    candidates: list[tuple[str, Decimal]] = []
    for symbol in _SONNET_TRADABLE:
        bars = bars_by_symbol.get(symbol, [])
        if not bars:
            continue
        sorted_bars = sorted(bars, key=lambda b: b.timestamp)
        closes = [b.close for b in sorted_bars]
        mom = _price_momentum(closes, _SONNET_MOM_LOOKBACK, _SONNET_MOM_SKIP)
        if mom is not None:
            candidates.append((symbol, mom))

    if not candidates:
        return {}

    # Highest momentum first
    candidates.sort(key=lambda x: x[1], reverse=True)
    selected = candidates[:top_n]

    weights: dict[str, Decimal] = {}
    for sym, _ in selected:
        weights[sym] = per_name
    # Sum may be < 0.5; remainder is cash in the backtest engine (no explicit cash symbol needed)
    return weights
