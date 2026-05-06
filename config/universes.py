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
