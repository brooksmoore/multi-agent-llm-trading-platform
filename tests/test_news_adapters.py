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


def test_edgar_parses_real_response_shape() -> None:
    """Production EDGAR responses use `display_names` + `form` (not the
    `entity_name`/`form_type` the adapter originally read). Verify the
    parser handles the real shape and produces a usable headline + URL.
    """
    mock_session = MagicMock()
    mock_session.get.return_value.json.return_value = {
        "hits": {
            "hits": [
                {
                    "_id": "0001045810-26-000026:nvda-20260424.htm",
                    "_source": {
                        "ciks": ["0001045810"],
                        "adsh": "0001045810-26-000026",
                        "display_names": ["NVIDIA CORP  (NVDA)  (CIK 0001045810)"],
                        "form": "8-K",
                        "file_date": "2026-04-27",
                        "items": ["5.02"],
                    },
                }
            ]
        }
    }
    mock_session.get.return_value.raise_for_status = MagicMock()
    adapter = EDGARAdapter(session=mock_session)
    items = adapter.get_filings("NVDA", form_type="8-K")
    assert len(items) == 1
    n = items[0]
    assert "8-K" in n.headline
    assert "NVIDIA" in n.headline
    assert "5.02" in n.headline
    # URL must be a real archive path: cik (no leading zeros) / adsh-no-dashes / filename.
    assert n.url == (
        "https://www.sec.gov/Archives/edgar/data/"
        "1045810/000104581026000026/nvda-20260424.htm"
    )
    assert n.source == NewsSource.EDGAR
    assert n.symbols == ("NVDA",)


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
    """Yahoo's current response shape: fields nested under `content`,
    `clickThroughUrl.url` for the link, ISO-8601 `pubDate`. Description is
    HTML and must be stripped into a plain-text summary."""
    mock_ticker = MagicMock()
    mock_ticker.news = [
        {
            "id": "abc",
            "content": {
                "title": "AMD beats Q1 estimates",
                "description": (
                    "<p>Advanced Micro Devices (<a href=\"...\">AMD</a>) posted "
                    "first quarter results that beat Wall Street estimates.</p>"
                ),
                "pubDate": "2026-05-05T20:42:03Z",
                "clickThroughUrl": {"url": "https://finance.yahoo.com/video/amd-q1.html"},
                "canonicalUrl": {"url": "https://finance.yahoo.com/video/amd-q1.html"},
                "contentType": "STORY",
            },
        }
    ]
    with patch("data.news.yf.Ticker", return_value=mock_ticker):
        adapter = YFinanceAdapter()
        items = adapter.get_news("AMD")
    assert len(items) == 1
    n = items[0]
    assert n.source == NewsSource.YFINANCE
    assert n.headline == "AMD beats Q1 estimates"
    assert n.url == "https://finance.yahoo.com/video/amd-q1.html"
    assert n.published_at.isoformat() == "2026-05-05T20:42:03+00:00"
    # HTML stripped, prose preserved
    assert n.summary is not None
    assert "<" not in n.summary
    assert "AMD" in n.summary
    assert "first quarter results" in n.summary


def test_yfinance_skips_legacy_shape() -> None:
    """Items missing the `content` wrapper (old shape) are skipped, not
    crashed on. Protects against partial responses during another API drift."""
    mock_ticker = MagicMock()
    mock_ticker.news = [
        # Legacy shape — no `content` key.
        {"title": "old", "link": "https://x", "providerPublishTime": 1737043200},
        # Valid current shape.
        {
            "content": {
                "title": "new",
                "pubDate": "2026-05-05T12:00:00Z",
                "canonicalUrl": {"url": "https://y"},
            },
        },
    ]
    with patch("data.news.yf.Ticker", return_value=mock_ticker):
        items = YFinanceAdapter().get_news("AMD")
    assert len(items) == 1
    assert items[0].headline == "new"


def test_yfinance_falls_back_to_canonical_url() -> None:
    """If clickThroughUrl is missing, canonicalUrl must be used."""
    mock_ticker = MagicMock()
    mock_ticker.news = [
        {
            "content": {
                "title": "t",
                "pubDate": "2026-05-05T12:00:00Z",
                "canonicalUrl": {"url": "https://canonical.example/article"},
            },
        }
    ]
    with patch("data.news.yf.Ticker", return_value=mock_ticker):
        items = YFinanceAdapter().get_news("AMD")
    assert items[0].url == "https://canonical.example/article"


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
