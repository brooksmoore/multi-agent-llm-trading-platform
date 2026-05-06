"""Sonnet's factor ranking must ignore ETFs and crypto carried by plumbing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from agents.sonnet_agent import SonnetAgent
from data.market import Bar


def _bars(symbol: str, n: int = 280) -> list[Bar]:
    """Return n daily bars with linearly increasing closes (positive 12-1 momentum)."""
    out: list[Bar] = []
    base = datetime(2025, 1, 1, tzinfo=UTC)
    for i in range(n):
        c = Decimal("100") + Decimal(i)
        out.append(
            Bar(
                symbol=symbol,
                timestamp=base + timedelta(days=i),
                open=c, high=c, low=c, close=c, volume=1000,
            )
        )
    return out


def test_factor_signals_filter_to_sonnet_universe() -> None:
    bars_by_symbol = {
        "AAPL": _bars("AAPL"),
        "NVDA": _bars("NVDA"),
        "SPY": _bars("SPY"),       # ETF — Haiku's, not Sonnet's
        "BTCUSD": _bars("BTCUSD"), # Crypto — Haiku's, not Sonnet's
        "TQQQ": _bars("TQQQ"),     # LETF — Haiku's tactical
    }
    agent = SonnetAgent.__new__(SonnetAgent)  # bypass __init__; we only test pure method
    signals = agent._compute_factor_signals(bars_by_symbol)
    assert "AAPL" in signals
    assert "NVDA" in signals
    assert "SPY" not in signals
    assert "BTCUSD" not in signals
    assert "TQQQ" not in signals
