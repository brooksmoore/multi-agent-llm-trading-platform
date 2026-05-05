"""News adapters: Finnhub, SEC EDGAR, RSS (feedparser), yfinance → NewsItem."""

from __future__ import annotations

import html
import re
import time
from datetime import UTC, datetime
from typing import Any

import feedparser
import requests
import yfinance as yf

from core.types import NewsItem, NewsSource


_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    """Remove HTML tags, decode entities, and collapse whitespace.

    Used by yfinance and EDGAR body extraction to turn HTML fragments into
    plain prose for NewsItem.summary / NewsItem.body. Entities like &nbsp;
    and &amp; are decoded — without this, body text reads as garbage and
    LLM tokenization wastes context on noise.
    """
    no_tags = _HTML_TAG_RE.sub(" ", text)
    decoded = html.unescape(no_tags)
    return _WS_RE.sub(" ", decoded).strip()


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
    # SEC publishes a 10 req/sec rate limit on edgar.sec.gov. We're well under
    # that since fetch_filings only fires during the Thu/Fri Opus deep-dive.
    _BODY_FETCH_TIMEOUT = 10
    _BODY_MAX_CHARS = 8000  # ~2 KB compressed; doc_pack uses body[:1000]

    def __init__(
        self,
        session: requests.Session | None = None,
        fetch_body: bool = True,
    ) -> None:
        self._session = session if session is not None else requests.Session()
        # Disable for tests / dry-runs to avoid the per-filing HTTPS round trip.
        self._fetch_body = fetch_body

    def _fetch_filing_body(self, url: str) -> str | None:
        """Download a filing document and return stripped text body, or None
        on any failure. Best-effort: a 4xx/5xx or timeout silently returns
        None so the parent get_filings still emits the headline+URL."""
        try:
            resp = self._session.get(
                url, headers=self._HEADERS, timeout=self._BODY_FETCH_TIMEOUT,
            )
            if resp.status_code != 200:
                return None
            text = _strip_html(resp.text)
            return text[: self._BODY_MAX_CHARS] if text else None
        except (requests.RequestException, ValueError):
            return None

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

                # SEC's full-text search index uses `display_names` + `form`,
                # not `entity_name`/`form_type`. The previous code read fields
                # that don't exist, so every headline came back as " — ".
                form = src.get("form") or src.get("file_type") or ""
                display = (src.get("display_names") or [""])[0]
                topic_items = src.get("items") or []
                topic_str = (
                    f" [Item {', '.join(topic_items)}]" if topic_items else ""
                )
                headline = f"{form} — {display}{topic_str}".strip(" —")

                # Build a real archive URL: edgar/data/<cik-int>/<adsh-no-dashes>/<filename>.
                # `_id` is shaped "<adsh>:<filename>"; `adsh` is also a top-level
                # field but we use _id as the canonical filename source.
                ciks = src.get("ciks") or []
                adsh = src.get("adsh") or ""
                hit_id = str(hit.get("_id") or "")
                filename = hit_id.split(":", 1)[1] if ":" in hit_id else ""
                if ciks and adsh and filename:
                    cik_int = int(ciks[0])
                    adsh_nodash = adsh.replace("-", "")
                    url = (
                        f"https://www.sec.gov/Archives/edgar/data/"
                        f"{cik_int}/{adsh_nodash}/{filename}"
                    )
                else:
                    # Fall back to the EDGAR file viewer for the accession number.
                    url = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={ciks[0] if ciks else ''}"

                # Best-effort body fetch — only when we built a real archive
                # URL (not the fallback browse-edgar landing page) and only
                # when fetch_body=True. Failure returns None and we still
                # persist headline+URL.
                body: str | None = None
                if self._fetch_body and url.startswith(
                    "https://www.sec.gov/Archives/"
                ):
                    body = self._fetch_filing_body(url)

                items.append(
                    NewsItem(
                        source=NewsSource.EDGAR,
                        headline=headline,
                        url=url,
                        published_at=published_at,
                        symbols=(symbol,),
                        body=body,
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
        """Pull recent news for a ticker.

        Yahoo restructured the response some time back: fields the legacy
        adapter read (`title`, `link`, `providerPublishTime`) no longer exist
        at the top level. Real shape is nested under `content` with
        `clickThroughUrl.url` / `canonicalUrl.url` for the link and
        `pubDate` (ISO 8601 string) for the timestamp. Without this fix the
        adapter silently returned [] for every call.
        """
        try:
            ticker = yf.Ticker(symbol)
            news_items: list[dict[str, Any]] = ticker.news
        except Exception:
            return []
        items: list[NewsItem] = []
        for item in news_items:
            try:
                content = item.get("content") or {}
                title = content.get("title")
                if not title:
                    continue

                url = (
                    (content.get("clickThroughUrl") or {}).get("url")
                    or (content.get("canonicalUrl") or {}).get("url")
                )
                if not url:
                    continue

                pub_str = content.get("pubDate") or content.get("displayTime")
                if not pub_str:
                    continue
                # ISO 8601 with trailing Z → +00:00 for fromisoformat.
                published_at = datetime.fromisoformat(
                    pub_str.replace("Z", "+00:00")
                )
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=UTC)

                desc = content.get("description") or ""
                stripped = _strip_html(desc) if desc else ""
                summary = stripped[:500] if stripped else None
                # Store the fuller description as body for Opus deep-dives.
                # doc_pack.py already emits body[:1000] under deep-dive items.
                # Cap at 4 KB to keep the news.db row size reasonable.
                body = stripped[:4000] if len(stripped) > 500 else None

                items.append(
                    NewsItem(
                        source=NewsSource.YFINANCE,
                        headline=str(title),
                        url=str(url),
                        published_at=published_at,
                        symbols=(symbol,),
                        summary=summary or None,
                        body=body,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return items
