"""Tests for data/news.py — Finnhub, EDGAR, RSS, yfinance adapters."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import requests

from core.types import NewsSource
from data.news import EDGARAdapter, FinnhubAdapter, RSSAdapter, YFinanceAdapter

_TS = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
_TS2 = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Finnhub
# ---------------------------------------------------------------------------


def test_finnhub_returns_news_items() -> None:
    mock_session = MagicMock()
    mock_session.get.return_value.json.return_value = [
        {
            "headline": "SPY rises",
            "summary": "good",
            "url": "https://a.com/1",
            "datetime": 1737043200,
            "source": "FinnHub",
        }
    ]
    mock_session.get.return_value.raise_for_status = MagicMock()
    adapter = FinnhubAdapter("key", session=mock_session)
    items = adapter.get_news("SPY", _TS, _TS2)
    assert len(items) == 1
    assert items[0].source == NewsSource.FINNHUB
    assert items[0].headline == "SPY rises"


def test_finnhub_network_error_returns_empty() -> None:
    mock_session = MagicMock()
    mock_session.get.side_effect = requests.RequestException("timeout")
    adapter = FinnhubAdapter("key", session=mock_session)
    items = adapter.get_news("SPY", _TS, _TS2)
    assert items == []


def test_finnhub_empty_response_returns_empty() -> None:
    mock_session = MagicMock()
    mock_session.get.return_value.json.return_value = []
    mock_session.get.return_value.raise_for_status = MagicMock()
    adapter = FinnhubAdapter("key", session=mock_session)
    items = adapter.get_news("SPY", _TS, _TS2)
    assert items == []


def test_finnhub_url_contains_symbol_and_dates() -> None:
    mock_session = MagicMock()
    mock_session.get.return_value.json.return_value = []
    mock_session.get.return_value.raise_for_status = MagicMock()
    adapter = FinnhubAdapter("key", session=mock_session)
    adapter.get_news("SPY", _TS, _TS2)
    call_kwargs = mock_session.get.call_args
    url: str = call_kwargs[0][0]
    assert "SPY" in call_kwargs[1].get("params", {}).get("symbol", url)


# ---------------------------------------------------------------------------
# EDGAR
# ---------------------------------------------------------------------------


def test_edgar_returns_filings() -> None:
    mock_session = MagicMock()
    mock_session.get.return_value.json.return_value = {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "form_type": "8-K",
                        "entity_name": "SPDR",
                        "file_date": "2026-01-15",
                    },
                    "_id": "0001234567890",
                }
            ]
        }
    }
    mock_session.get.return_value.raise_for_status = MagicMock()
    adapter = EDGARAdapter(session=mock_session)
    items = adapter.get_filings("SPY")
    assert len(items) == 1
    assert items[0].source == NewsSource.EDGAR


def test_edgar_network_error_returns_empty() -> None:
    mock_session = MagicMock()
    mock_session.get.side_effect = requests.RequestException("network error")
    adapter = EDGARAdapter(session=mock_session)
    items = adapter.get_filings("SPY")
    assert items == []


def test_edgar_empty_hits_returns_empty() -> None:
    mock_session = MagicMock()
    mock_session.get.return_value.json.return_value = {"hits": {"hits": []}}
    mock_session.get.return_value.raise_for_status = MagicMock()
    adapter = EDGARAdapter(session=mock_session)
    items = adapter.get_filings("SPY")
    assert items == []


def test_edgar_form_type_in_params() -> None:
    mock_session = MagicMock()
    mock_session.get.return_value.json.return_value = {"hits": {"hits": []}}
    mock_session.get.return_value.raise_for_status = MagicMock()
    adapter = EDGARAdapter(session=mock_session)
    adapter.get_filings("SPY", form_type="8-K")
    call_kwargs = mock_session.get.call_args
    params: dict[str, str] = call_kwargs[1].get("params", {})
    assert params.get("forms") == "8-K"


# ---------------------------------------------------------------------------
# RSS
# ---------------------------------------------------------------------------


def test_rss_returns_news_items() -> None:
    mock_entry = MagicMock()
    mock_entry.title = "Market update"
    mock_entry.link = "https://b.com/1"
    mock_entry.published_parsed = (2026, 1, 15, 12, 0, 0, 0, 0, 0)
    mock_entry.summary = "brief"

    mock_feed = MagicMock()
    mock_feed.entries = [mock_entry]

    with patch("data.news.feedparser.parse", return_value=mock_feed):
        adapter = RSSAdapter(["https://b.com/rss"])
        items = adapter.get_news()
    assert len(items) == 1
    assert items[0].source == NewsSource.RSS


def test_rss_bad_feed_skipped() -> None:
    good_entry = MagicMock()
    good_entry.title = "Good news"
    good_entry.link = "https://good.com/1"
    good_entry.published_parsed = (2026, 1, 15, 12, 0, 0, 0, 0, 0)
    good_entry.summary = "good"

    good_feed = MagicMock()
    good_feed.entries = [good_entry]

    def parse_side_effect(url: str) -> MagicMock:
        if "bad" in url:
            raise RuntimeError("bad feed")
        return good_feed

    with patch("data.news.feedparser.parse", side_effect=parse_side_effect):
        adapter = RSSAdapter(["https://good.com/rss", "https://bad.com/rss"])
        items = adapter.get_news()
    assert len(items) == 1


def test_rss_no_published_parsed_uses_now() -> None:
    mock_entry = MagicMock()
    mock_entry.title = "No date"
    mock_entry.link = "https://c.com/1"
    mock_entry.published_parsed = None
    mock_entry.summary = ""

    mock_feed = MagicMock()
    mock_feed.entries = [mock_entry]

    with patch("data.news.feedparser.parse", return_value=mock_feed):
        adapter = RSSAdapter(["https://c.com/rss"])
        items = adapter.get_news()
    assert len(items) == 1
    assert isinstance(items[0].published_at, datetime)


def test_rss_deduplicates_by_url() -> None:
    def make_entry() -> MagicMock:
        entry = MagicMock()
        entry.title = "Same"
        entry.link = "https://shared.com/1"
        entry.published_parsed = (2026, 1, 15, 12, 0, 0, 0, 0, 0)
        entry.summary = "dup"
        return entry

    feed1 = MagicMock()
    feed1.entries = [make_entry()]
    feed2 = MagicMock()
    feed2.entries = [make_entry()]

    call_count = 0

    def parse_side_effect(url: str) -> MagicMock:
        nonlocal call_count
        result = feed1 if call_count == 0 else feed2
        call_count += 1
        return result

    with patch("data.news.feedparser.parse", side_effect=parse_side_effect):
        adapter = RSSAdapter(["https://feed1.com/rss", "https://feed2.com/rss"])
        items = adapter.get_news()
    assert len(items) == 1


# ---------------------------------------------------------------------------
# YFinance
# ---------------------------------------------------------------------------


def test_yfinance_returns_news() -> None:
    mock_ticker = MagicMock()
    mock_ticker.news = [
        {
            "title": "Test",
            "link": "https://c.com/1",
            "providerPublishTime": 1737043200,
            "publisher": "Yahoo",
        }
    ]
    with patch("data.news.yf.Ticker", return_value=mock_ticker):
        adapter = YFinanceAdapter()
        items = adapter.get_news("SPY")
    assert len(items) == 1
    assert items[0].source == NewsSource.YFINANCE


def test_yfinance_error_returns_empty() -> None:
    mock_ticker = MagicMock()
    type(mock_ticker).news = property(lambda self: (_ for _ in ()).throw(AttributeError()))
    with patch("data.news.yf.Ticker", return_value=mock_ticker):
        adapter = YFinanceAdapter()
        items = adapter.get_news("SPY")
    assert items == []


def test_yfinance_empty_returns_empty() -> None:
    mock_ticker = MagicMock()
    mock_ticker.news = []
    with patch("data.news.yf.Ticker", return_value=mock_ticker):
        adapter = YFinanceAdapter()
        items = adapter.get_news("SPY")
    assert items == []
