"""Per-agent tradable universes + the plumbing union.

The plumbing universe (union of all sleeves) is what the system maintains
data for: bars, news, marks, drawdown tracking. Each agent's *strategy*
universe is a strict subset, applied at the prompt/factor-computation layer
so each LLM only sees symbols it can actually trade.

Editing rules:
- Symbols only appear in PLUMBING_UNIVERSE if at least one strategy can
  trade them. Don't add "for completeness."
- Strategy universes are append-only at the boundary; removing names
  invalidates calibration history for that name.
"""

from __future__ import annotations

from decimal import Decimal

# ─── Haiku (trend-follower) ───────────────────────────────────────────────────

HAIKU_ETF_UNIVERSE: list[str] = [
    "SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "USO", "VNQ",
]

HAIKU_LETF_UNIVERSE: list[str] = ["TQQQ", "SQQQ", "UPRO", "SOXL"]

HAIKU_CRYPTO_UNIVERSE: list[str] = ["BTCUSD", "ETHUSD", "SOLUSD"]

# ─── Sonnet (cross-sectional 12-1 momentum) ───────────────────────────────────

# ~55 sector-diversified large/mid caps — wide enough for the factor to rank
# meaningfully without overwhelming the daily-bar data layer.
SONNET_EQUITY_UNIVERSE: list[str] = [
    # Tech
    "AAPL", "NVDA", "MSFT", "GOOGL", "AMZN", "META", "AVGO", "ORCL", "CRM", "ADBE",
    # Financials
    "JPM", "BAC", "GS", "MS", "BLK", "V", "MA", "AXP", "SCHW",
    # Healthcare
    "UNH", "JNJ", "LLY", "PFE", "ABBV", "TMO", "MRK", "DHR", "ISRG",
    # Consumer disc
    "TSLA", "HD", "MCD", "NKE", "LOW", "SBUX", "BKNG",
    # Consumer staples
    "WMT", "COST", "PG", "KO", "PEP",
    # Energy
    "XOM", "CVX", "COP", "EOG",
    # Industrials
    "GE", "BA", "CAT", "HON", "UPS", "RTX",
    # Communication
    "NFLX", "DIS", "VZ", "CMCSA",
    # Utilities / Materials / REITs
    "NEE", "LIN", "PLD", "AMT",
]

# ─── Small/mid-cap growth slice — Sonnet *and* Opus tradable ──────────────────

GROWTH_SLICE_UNIVERSE: list[str] = [
    "RBLX", "HIMS", "CELH", "SOFI", "PLTR", "DDOG", "NET", "CRWD",
    "SNOW", "MDB", "AFRM", "SHOP", "COIN", "RIVN", "PINS",
]

# ─── Opus (concentrated discretionary) ────────────────────────────────────────

# Opus draws from the same pool as Sonnet plus the growth slice. Opus may
# also propose names outside this list via watchlist_add, which then go
# through an explicit "promote to plumbing" review step.
OPUS_EQUITY_UNIVERSE: list[str] = [
    *SONNET_EQUITY_UNIVERSE,
    *GROWTH_SLICE_UNIVERSE,
]

# ─── Plumbing union ───────────────────────────────────────────────────────────

def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


PLUMBING_UNIVERSE: list[str] = _dedupe_preserve_order([
    *HAIKU_ETF_UNIVERSE,
    *HAIKU_LETF_UNIVERSE,
    *HAIKU_CRYPTO_UNIVERSE,
    *SONNET_EQUITY_UNIVERSE,
    *GROWTH_SLICE_UNIVERSE,
])


# ─── Liquidity tiers + estimated per-side slippage ────────────────────────────
#
# The OMS/planner currently treats slippage as zero, which silently flatters
# the lower-liquidity end of the universe (the GROWTH_SLICE — HIMS, AFRM,
# RIVN, PINS, RBLX, etc.). These tier defaults are deliberately conservative
# starting estimates, *one-sided* per fill (round-trip = 2× these numbers).
#
# IMPORTANT — these are guesses until they're calibrated against actual paper
# fills. After ~1 week of live paper trading, run a per-symbol comparison of
# fill price vs. mark-at-submission and replace these with empirical numbers.
# Until then the values are surfaced via planner logs (not yet baked into the
# sizing math) so they don't bias outcomes — see planner.py for the wiring.
#
# Numbers are basis points. 10 bps = 0.10% of notional.

_TIER_MEGA = "mega"      # >$200B mkt cap megacaps + biggest broad ETFs
_TIER_LARGE = "large"    # $20-200B large caps, sector ETFs
_TIER_MID = "mid"        # $2-20B mid caps
_TIER_SMALL = "small"    # <$2B or known wide-spread retail growth names
_TIER_LETF = "letf"      # leveraged ETFs (TQQQ etc) — tight despite vol
_TIER_CRYPTO = "crypto"  # Alpaca crypto — fee already deducted in-kind, this
                         # is spread only; the 0.25% taker is handled in
                         # alpaca_broker._translate_order

SLIPPAGE_BPS_BY_TIER: dict[str, Decimal] = {
    _TIER_MEGA:   Decimal("2"),
    _TIER_LARGE:  Decimal("4"),
    _TIER_MID:    Decimal("8"),
    _TIER_SMALL:  Decimal("18"),
    _TIER_LETF:   Decimal("6"),
    _TIER_CRYPTO: Decimal("8"),
}

# Per-symbol tier assignment. Anything not in this map falls back to LARGE
# (the median assumption) so a typo doesn't silently get the cheapest tier.
_SYMBOL_TIER: dict[str, str] = {
    # Mega-cap broad ETFs
    "SPY": _TIER_MEGA, "QQQ": _TIER_MEGA, "IWM": _TIER_LARGE,
    "EFA": _TIER_LARGE, "EEM": _TIER_LARGE,
    "TLT": _TIER_LARGE, "IEF": _TIER_LARGE,
    "GLD": _TIER_LARGE, "USO": _TIER_LARGE, "VNQ": _TIER_LARGE,
    # LETFs
    "TQQQ": _TIER_LETF, "SQQQ": _TIER_LETF,
    "UPRO": _TIER_LETF, "SOXL": _TIER_LETF,
    # Crypto
    "BTCUSD": _TIER_CRYPTO, "ETHUSD": _TIER_CRYPTO, "SOLUSD": _TIER_CRYPTO,
    # Mega-cap single names (>$500B-ish)
    "AAPL": _TIER_MEGA, "NVDA": _TIER_MEGA, "MSFT": _TIER_MEGA,
    "GOOGL": _TIER_MEGA, "AMZN": _TIER_MEGA, "META": _TIER_MEGA,
    # Large-cap single names
    "AVGO": _TIER_LARGE, "ORCL": _TIER_LARGE, "CRM": _TIER_LARGE, "ADBE": _TIER_LARGE,
    "JPM": _TIER_LARGE, "BAC": _TIER_LARGE, "GS": _TIER_LARGE, "MS": _TIER_LARGE,
    "BLK": _TIER_LARGE, "V": _TIER_LARGE, "MA": _TIER_LARGE,
    "AXP": _TIER_LARGE, "SCHW": _TIER_LARGE,
    "UNH": _TIER_LARGE, "JNJ": _TIER_LARGE, "LLY": _TIER_LARGE, "PFE": _TIER_LARGE,
    "ABBV": _TIER_LARGE, "TMO": _TIER_LARGE, "MRK": _TIER_LARGE,
    "DHR": _TIER_LARGE, "ISRG": _TIER_LARGE,
    "TSLA": _TIER_LARGE, "HD": _TIER_LARGE, "MCD": _TIER_LARGE, "NKE": _TIER_LARGE,
    "LOW": _TIER_LARGE, "SBUX": _TIER_LARGE, "BKNG": _TIER_LARGE,
    "WMT": _TIER_LARGE, "COST": _TIER_LARGE, "PG": _TIER_LARGE,
    "KO": _TIER_LARGE, "PEP": _TIER_LARGE,
    "XOM": _TIER_LARGE, "CVX": _TIER_LARGE, "COP": _TIER_LARGE, "EOG": _TIER_LARGE,
    "GE": _TIER_LARGE, "BA": _TIER_LARGE, "CAT": _TIER_LARGE,
    "HON": _TIER_LARGE, "UPS": _TIER_LARGE, "RTX": _TIER_LARGE,
    "NFLX": _TIER_LARGE, "DIS": _TIER_LARGE,
    "VZ": _TIER_LARGE, "CMCSA": _TIER_LARGE,
    "NEE": _TIER_LARGE, "LIN": _TIER_LARGE, "PLD": _TIER_LARGE, "AMT": _TIER_LARGE,
    # Growth slice — mid/small with wider spreads
    "DDOG": _TIER_MID, "NET": _TIER_MID, "CRWD": _TIER_MID,
    "SNOW": _TIER_MID, "MDB": _TIER_MID, "PLTR": _TIER_MID,
    "SHOP": _TIER_MID, "COIN": _TIER_MID,
    "RBLX": _TIER_SMALL, "HIMS": _TIER_SMALL, "CELH": _TIER_SMALL,
    "SOFI": _TIER_SMALL, "AFRM": _TIER_SMALL, "RIVN": _TIER_SMALL,
    "PINS": _TIER_SMALL,
}


def liquidity_tier(symbol: str) -> str:
    """Tier assignment for a tradable symbol. Falls back to LARGE for anything
    not explicitly classified — biases conservatively (median) rather than
    optimistically (mega) so a missing entry can't quietly under-price slippage.
    """
    return _SYMBOL_TIER.get(symbol, _TIER_LARGE)


def estimated_slippage_bps(symbol: str) -> Decimal:
    """One-sided estimated slippage in basis points for a market-order fill.

    Round-trip cost is roughly 2× this. Values are starting estimates only,
    pending paper-trade calibration — see SLIPPAGE_BPS_BY_TIER comment.
    """
    return SLIPPAGE_BPS_BY_TIER[liquidity_tier(symbol)]
