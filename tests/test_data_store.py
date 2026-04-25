"""Tests for data/store.py — DuckDB-backed bar and news storage."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pytest

from core.types import NewsItem, NewsSource
from data.market import Bar
from data.store import DataStore

UTC: Final = UTC

_TS: Final = datetime(2026, 1, 2, 15, 30, tzinfo=UTC)
_TS2: Final = datetime(2026, 1, 3, 15, 30, tzinfo=UTC)
_TS3: Final = datetime(2026, 1, 4, 15, 30, tzinfo=UTC)


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


def _news(symbol: str = "SPY", ts: datetime = _TS) -> NewsItem:
    return NewsItem(
        source=NewsSource.FINNHUB,
        headline="Test headline",
        url=f"https://example.com/{symbol}/{ts.isoformat()}",
        published_at=ts,
        symbols=(symbol,),
    )


def test_save_and_load_single_bar() -> None:
    store = DataStore()
    b = _bar()
    store.save_bars([b])
    bars = store.load_bars("SPY", _TS - timedelta(seconds=1), _TS + timedelta(seconds=1))
    assert len(bars) == 1
    loaded = bars[0]
    assert loaded.symbol == b.symbol
    assert loaded.timestamp == b.timestamp
    assert loaded.open == b.open
    assert loaded.high == b.high
    assert loaded.low == b.low
    assert loaded.close == b.close
    assert loaded.volume == b.volume
    assert loaded.vwap == b.vwap
    store.close()


def test_save_multiple_bars() -> None:
    store = DataStore()
    store.save_bars([_bar(ts=_TS), _bar(ts=_TS2), _bar(ts=_TS3)])
    bars = store.load_bars("SPY", _TS - timedelta(seconds=1), _TS3 + timedelta(seconds=1))
    assert len(bars) == 3
    store.close()


def test_load_bars_date_filter() -> None:
    store = DataStore()
    store.save_bars([_bar(ts=_TS), _bar(ts=_TS2), _bar(ts=_TS3)])
    bars = store.load_bars("SPY", _TS, _TS2)
    assert len(bars) == 2
    store.close()


def test_load_bars_symbol_filter() -> None:
    store = DataStore()
    store.save_bars([_bar(symbol="SPY", ts=_TS), _bar(symbol="QQQ", ts=_TS)])
    bars = store.load_bars("SPY", _TS - timedelta(seconds=1), _TS + timedelta(seconds=1))
    assert len(bars) == 1
    assert bars[0].symbol == "SPY"
    store.close()


def test_save_bars_upsert() -> None:
    store = DataStore()
    store.save_bars([_bar(close="450.00")])
    store.save_bars([_bar(close="460.00")])
    bars = store.load_bars("SPY", _TS - timedelta(seconds=1), _TS + timedelta(seconds=1))
    assert len(bars) == 1
    assert bars[0].close == Decimal("460.00")
    store.close()


def test_load_bars_empty() -> None:
    store = DataStore()
    bars = store.load_bars("SPY", _TS, _TS2)
    assert bars == []
    store.close()


def test_load_bars_ordered() -> None:
    store = DataStore()
    store.save_bars([_bar(ts=_TS3), _bar(ts=_TS), _bar(ts=_TS2)])
    bars = store.load_bars("SPY", _TS - timedelta(seconds=1), _TS3 + timedelta(seconds=1))
    assert len(bars) == 3
    assert bars[0].timestamp == _TS
    assert bars[1].timestamp == _TS2
    assert bars[2].timestamp == _TS3
    store.close()


def test_save_and_load_bar_with_vwap() -> None:
    store = DataStore()
    b = Bar(
        symbol="SPY",
        timestamp=_TS,
        open=Decimal("440.00"),
        high=Decimal("455.00"),
        low=Decimal("438.00"),
        close=Decimal("450.00"),
        volume=1_000_000,
        vwap=Decimal("449.75"),
    )
    store.save_bars([b])
    bars = store.load_bars("SPY", _TS - timedelta(seconds=1), _TS + timedelta(seconds=1))
    assert len(bars) == 1
    assert bars[0].vwap == Decimal("449.75")
    store.close()


def test_bar_vwap_none_round_trips() -> None:
    store = DataStore()
    store.save_bars([_bar()])
    bars = store.load_bars("SPY", _TS - timedelta(seconds=1), _TS + timedelta(seconds=1))
    assert len(bars) == 1
    assert bars[0].vwap is None
    store.close()


def test_save_and_load_news() -> None:
    store = DataStore()
    item = _news()
    store.save_news([item])
    news = store.load_news("SPY", _TS - timedelta(seconds=1), _TS + timedelta(seconds=1))
    assert len(news) == 1
    loaded = news[0]
    assert loaded.url == item.url
    assert loaded.headline == item.headline
    assert loaded.source == item.source
    assert loaded.published_at == item.published_at
    assert loaded.symbols == item.symbols
    store.close()


def test_load_news_symbol_filter() -> None:
    store = DataStore()
    store.save_news([_news(symbol="SPY", ts=_TS), _news(symbol="QQQ", ts=_TS)])
    news = store.load_news("SPY", _TS - timedelta(seconds=1), _TS + timedelta(seconds=1))
    assert len(news) == 1
    assert "SPY" in news[0].symbols
    store.close()


def test_load_news_date_filter() -> None:
    store = DataStore()
    store.save_news([_news(ts=_TS), _news(ts=_TS3)])
    news = store.load_news("SPY", _TS, _TS2)
    assert len(news) == 1
    assert news[0].published_at == _TS
    store.close()


def test_save_news_upsert() -> None:
    store = DataStore()
    url = f"https://example.com/SPY/{_TS.isoformat()}"
    item1 = NewsItem(
        source=NewsSource.FINNHUB,
        headline="First headline",
        url=url,
        published_at=_TS,
        symbols=("SPY",),
    )
    item2 = NewsItem(
        source=NewsSource.FINNHUB,
        headline="Updated headline",
        url=url,
        published_at=_TS,
        symbols=("SPY",),
    )
    store.save_news([item1])
    store.save_news([item2])
    news = store.load_news("SPY", _TS - timedelta(seconds=1), _TS + timedelta(seconds=1))
    assert len(news) == 1
    assert news[0].headline == "Updated headline"
    store.close()


def test_load_news_with_sentiment() -> None:
    store = DataStore()
    item = NewsItem(
        source=NewsSource.FINNHUB,
        headline="Positive news",
        url="https://example.com/sentiment",
        published_at=_TS,
        symbols=("SPY",),
        sentiment=0.75,
    )
    store.save_news([item])
    news = store.load_news("SPY", _TS - timedelta(seconds=1), _TS + timedelta(seconds=1))
    assert len(news) == 1
    assert news[0].sentiment == pytest.approx(0.75)
    store.close()


def test_news_multiple_symbols() -> None:
    store = DataStore()
    item = NewsItem(
        source=NewsSource.FINNHUB,
        headline="Multi-symbol news",
        url="https://example.com/multi",
        published_at=_TS,
        symbols=("SPY", "QQQ"),
    )
    store.save_news([item])
    news = store.load_news("QQQ", _TS - timedelta(seconds=1), _TS + timedelta(seconds=1))
    assert len(news) == 1
    assert "QQQ" in news[0].symbols
    assert "SPY" in news[0].symbols
    store.close()


def test_close_and_reopen(tmp_path: Path) -> None:
    db_path = str(tmp_path / "test.duckdb")
    store = DataStore(db_path)
    store.save_bars([_bar()])
    store.close()
    store2 = DataStore(db_path)
    bars = store2.load_bars("SPY", _TS - timedelta(seconds=1), _TS + timedelta(seconds=1))
    store2.close()
    assert len(bars) == 1
