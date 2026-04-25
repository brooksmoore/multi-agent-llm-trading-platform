"""News adapters: Finnhub, SEC EDGAR, RSS (feedparser), yfinance → NewsItem."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import feedparser
import requests
import yfinance as yf

from core.types import NewsItem, NewsSource


class FinnhubAdapter:
    _BASE = "https://finnhub.io/api/v1"

    def __init__(self, api_key: str, session: requests.Session | None = None) -> None:
        self._api_key = api_key
        self._session = session if session is not None else requests.Session()

    def get_news(
        self, symbol: str, from_dt: datetime, to_dt: datetime
    ) -> list[NewsItem]:
        url = f"{self._BASE}/company-news"
        params: dict[str, str] = {
            "symbol": symbol,
            "from": str(from_dt.date()),
            "to": str(to_dt.date()),
            "token": self._api_key,
        }
        try:
            response = self._session.get(url, params=params)
            response.raise_for_status()
            data: list[dict[str, Any]] = response.json()
        except (requests.RequestException, ValueError):
            return []
        items: list[NewsItem] = []
        for item in data:
            try:
                items.append(
                    NewsItem(
                        source=NewsSource.FINNHUB,
                        headline=item["headline"],
                        url=item["url"],
                        published_at=datetime.fromtimestamp(item["datetime"], UTC),
                        symbols=(symbol,),
                        summary=item.get("summary"),
                        sentiment=None,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return items


class EDGARAdapter:
    _BASE = "https://efts.sec.gov/LATEST/search-index"
    _HEADERS = {"User-Agent": "Multi-Agent-Bot/1.0 bcm3000@gmail.com"}

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session if session is not None else requests.Session()

    def get_filings(
        self,
        symbol: str,
        form_type: str = "8-K",
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        limit: int = 10,
    ) -> list[NewsItem]:
        params: dict[str, str] = {
            "q": symbol,
            "forms": form_type,
        }
        if from_dt is not None and to_dt is not None:
            params["dateRange"] = "custom"
            params["startdt"] = from_dt.date().isoformat()
            params["enddt"] = to_dt.date().isoformat()
        try:
            response = self._session.get(self._BASE, params=params, headers=self._HEADERS)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        except (requests.RequestException, ValueError):
            return []
        items: list[NewsItem] = []
        hits: list[dict[str, Any]] = data.get("hits", {}).get("hits", [])
        for hit in hits[:limit]:
            try:
                src = hit["_source"]
                published_at = datetime.fromisoformat(src["file_date"]).replace(tzinfo=UTC)
                headline = (
                    f"{src.get('form_type', '')} — {src.get('entity_name', '')}"
                )
                url = f"https://www.sec.gov/Archives/edgar/data/{hit['_id']}"
                items.append(
                    NewsItem(
                        source=NewsSource.EDGAR,
                        headline=headline,
                        url=url,
                        published_at=published_at,
                        symbols=(symbol,),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return items


class RSSAdapter:
    def __init__(self, feed_urls: list[str]) -> None:
        self._feed_urls = feed_urls

    def get_news(self, symbols: tuple[str, ...] = ()) -> list[NewsItem]:
        seen_urls: set[str] = set()
        items: list[NewsItem] = []
        for url in self._feed_urls:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries:
                    entry_url: str = getattr(entry, "link", "")
                    if entry_url in seen_urls:
                        continue
                    seen_urls.add(entry_url)
                    published_parsed: time.struct_time | None = getattr(
                        entry, "published_parsed", None
                    )
                    if published_parsed is not None:
                        published_at = datetime(*published_parsed[:6], tzinfo=UTC)
                    else:
                        published_at = datetime.now(UTC)
                    raw_summary: str = ""
                    if hasattr(entry, "summary"):
                        raw_summary = (entry.summary or "")[:500]
                    items.append(
                        NewsItem(
                            source=NewsSource.RSS,
                            headline=getattr(entry, "title", ""),
                            url=entry_url,
                            published_at=published_at,
                            symbols=symbols,
                            summary=raw_summary if raw_summary else None,
                        )
                    )
            except Exception:
                continue
        return items


class YFinanceAdapter:
    def get_news(self, symbol: str) -> list[NewsItem]:
        try:
            ticker = yf.Ticker(symbol)
            news_items: list[dict[str, Any]] = ticker.news
            items: list[NewsItem] = []
            for item in news_items:
                try:
                    items.append(
                        NewsItem(
                            source=NewsSource.YFINANCE,
                            headline=item["title"],
                            url=item["link"],
                            published_at=datetime.fromtimestamp(
                                item["providerPublishTime"], UTC
                            ),
                            symbols=(symbol,),
                        )
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            return items
        except Exception:
            return []
