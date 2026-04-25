"""Tests for data/summarize.py — BriefingSummarizer."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from core.types import NewsItem, NewsSource
from data.market import Bar
from data.summarize import BriefingSummarizer

_TS = datetime(2026, 1, 2, tzinfo=UTC)


def _bar(
    symbol: str = "SPY",
    ts: datetime = _TS,
    close: str = "450.00",
) -> Bar:
    return Bar(
        symbol=symbol,
        timestamp=ts,
        open=Decimal("440.00"),
        high=Decimal("455.00"),
        low=Decimal("438.00"),
        close=Decimal(close),
        volume=1_000_000,
    )


def _news(headline: str = "Test") -> NewsItem:
    return NewsItem(
        source=NewsSource.FINNHUB,
        headline=headline,
        url="https://x.com/1",
        published_at=_TS,
        symbols=("SPY",),
    )


def test_summarize_bars_contains_symbol() -> None:
    summarizer = BriefingSummarizer()
    result = summarizer.summarize_bars([_bar()], "SPY")
    assert "SPY" in result


def test_summarize_bars_truncates() -> None:
    summarizer = BriefingSummarizer()
    bars = [_bar() for _ in range(50)]
    full = summarizer.summarize_bars(bars, "SPY")
    result = summarizer.summarize_bars(bars, "SPY", max_chars=50)
    assert len(result) < len(full)


def test_summarize_news_contains_headline() -> None:
    summarizer = BriefingSummarizer()
    result = summarizer.summarize_news([_news("Big earnings beat")])
    assert "Big earnings beat" in result


def test_summarize_news_empty() -> None:
    summarizer = BriefingSummarizer()
    result = summarizer.summarize_news([])
    assert len(result) > 0


def test_build_market_brief_contains_all_symbols() -> None:
    summarizer = BriefingSummarizer()
    bars_by_symbol = {
        "SPY": [_bar("SPY")],
        "QQQ": [_bar("QQQ")],
    }
    result = summarizer.build_market_brief(bars_by_symbol, [])
    assert "SPY" in result
    assert "QQQ" in result


def test_bar_with_vwap_shown() -> None:
    summarizer = BriefingSummarizer()
    bar = Bar(
        symbol="SPY",
        timestamp=_TS,
        open=Decimal("440.00"),
        high=Decimal("455.00"),
        low=Decimal("438.00"),
        close=Decimal("450.00"),
        volume=1_000_000,
        vwap=Decimal("449.50"),
    )
    result = summarizer.summarize_bars([bar], "SPY")
    assert "VWAP" in result
