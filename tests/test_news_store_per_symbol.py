"""Tests for NewsStore.recent_for_symbols per-symbol cap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.types import NewsItem, NewsSource
from data.news_store import NewsStore


def _item(symbol: str, headline: str, minutes_ago: int) -> NewsItem:
    return NewsItem(
        source=NewsSource.YFINANCE,
        headline=headline,
        url=f"https://example.com/{headline.replace(' ', '_')}",
        published_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        symbols=(symbol,),
    )


def test_per_symbol_limit_caps_hot_names(tmp_path: Path) -> None:
    """20 NVDA headlines + 1 SPY headline: per_symbol_limit=3 must keep SPY visible."""
    store = NewsStore(tmp_path / "news.db")
    items = [_item("NVDA", f"nvda {i}", minutes_ago=i) for i in range(20)]
    items.append(_item("SPY", "spy headline", minutes_ago=21))
    store.add_items(items)

    since = datetime.now(UTC) - timedelta(hours=1)
    results = store.recent_for_symbols(
        symbols=["NVDA", "SPY"], since=since, limit=80, per_symbol_limit=3,
    )

    by_sym = {sym: 0 for sym in ["NVDA", "SPY"]}
    for it in results:
        for s in it.symbols:
            if s in by_sym:
                by_sym[s] += 1

    assert by_sym["NVDA"] == 3, f"NVDA should be capped at 3, got {by_sym['NVDA']}"
    assert by_sym["SPY"] == 1, f"SPY should still appear once, got {by_sym['SPY']}"


def test_no_per_symbol_limit_preserves_old_behavior(tmp_path: Path) -> None:
    store = NewsStore(tmp_path / "news.db")
    items = [_item("NVDA", f"n{i}", minutes_ago=i) for i in range(10)]
    store.add_items(items)

    since = datetime.now(UTC) - timedelta(hours=1)
    results = store.recent_for_symbols(symbols=["NVDA"], since=since, limit=10)
    assert len(results) == 10
