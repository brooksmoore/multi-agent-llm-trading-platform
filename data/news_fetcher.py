"""Orchestrator that pulls news from the four adapters and persists to NewsStore.

Designed to be called from a background thread or APScheduler job. Failures
in any single adapter are isolated — partial fetches still persist what they
got. The fetcher does not call the LLM; it is pure data plumbing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from core.types import NewsItem
from data.news import EDGARAdapter, FinnhubAdapter, RSSAdapter, YFinanceAdapter
from data.news_store import NewsStore

log = logging.getLogger(__name__)

# Default RSS feeds — broad market coverage, free, no API key required.
DEFAULT_RSS_FEEDS: list[str] = [
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",         # CNBC top news
    "https://www.cnbc.com/id/10001147/device/rss/rss.html",          # CNBC markets
    "https://feeds.marketwatch.com/marketwatch/topstories/",         # MarketWatch
    "https://feeds.marketwatch.com/marketwatch/marketpulse/",
    "https://www.federalreserve.gov/feeds/press_all.xml",            # Fed press
]


class NewsFetcher:
    """Fetches news from all configured adapters and writes to NewsStore.

    Adapters are optional — pass None for any you don't have credentials for.
    """

    def __init__(
        self,
        store: NewsStore,
        finnhub: FinnhubAdapter | None = None,
        edgar: EDGARAdapter | None = None,
        rss: RSSAdapter | None = None,
        yfinance: YFinanceAdapter | None = None,
    ) -> None:
        self._store = store
        self._finnhub = finnhub
        self._edgar = edgar
        self._rss = rss
        self._yfinance = yfinance

    def fetch_for_universe(
        self, symbols: list[str], lookback_days: int = 2
    ) -> int:
        """Pull recent news for each symbol from every configured adapter.

        Returns the number of newly-persisted items (dedup by URL).
        Exceptions inside individual adapters are caught and logged.
        """
        now = datetime.now(UTC)
        from_dt = now - timedelta(days=lookback_days)
        all_items: list[NewsItem] = []

        # RSS is universe-wide (not per-symbol) — fetch once. We tag every RSS
        # item with the broad-market macro proxies (SPY/TLT/GLD) so the Manager's
        # macro_snapshot query can find them. The tagging is semantic, not literal:
        # a Fed press release is "about" the bond market and broad equities.
        if self._rss is not None:
            try:
                rss_items = self._rss.get_news(symbols=("SPY", "TLT", "GLD"))
                rss_items = [i for i in rss_items if i.published_at >= from_dt]
                all_items.extend(rss_items)
            except Exception:
                log.exception("RSS fetch failed")

        # Per-symbol adapters. Only equity tickers — skip crypto symbols
        # (they end in USD and Finnhub/EDGAR have no useful coverage).
        equity_symbols = [s for s in symbols if not s.endswith("USD")]
        for sym in equity_symbols:
            if self._finnhub is not None:
                try:
                    all_items.extend(self._finnhub.get_news(sym, from_dt, now))
                except Exception:
                    log.warning("Finnhub fetch failed for %s", sym, exc_info=True)
            if self._yfinance is not None:
                try:
                    all_items.extend(self._yfinance.get_news(sym))
                except Exception:
                    log.warning("yfinance fetch failed for %s", sym, exc_info=True)

        inserted = self._store.add_items(all_items)
        log.info(
            "news fetch: %d adapters touched, %d items collected, %d newly inserted",
            sum(a is not None for a in (self._finnhub, self._edgar, self._rss, self._yfinance)),
            len(all_items),
            inserted,
        )
        return inserted

    def fetch_filings(self, symbols: list[str], lookback_days: int = 90) -> int:
        """Pull recent SEC filings (8-K + 10-Q) for the given symbols.

        Used by the Opus deep-dive doc_pack builder, not by the regular
        intraday news fetch. Returns rows newly inserted.
        """
        if self._edgar is None:
            return 0
        now = datetime.now(UTC)
        from_dt = now - timedelta(days=lookback_days)
        all_items: list[NewsItem] = []
        for sym in symbols:
            for form in ("8-K", "10-Q"):
                try:
                    all_items.extend(
                        self._edgar.get_filings(
                            sym, form_type=form, from_dt=from_dt, to_dt=now, limit=10
                        )
                    )
                except Exception:
                    log.warning(
                        "EDGAR fetch failed for %s %s", sym, form, exc_info=True
                    )
        return self._store.add_items(all_items)
