"""SPY benchmark fetcher for the dashboard.

Pulls daily SPY bars from Alpaca and caches them for 5 minutes. Used to overlay
a SPY-equivalent line on the NAV curve so we can see whether the bot is
beating buy-and-hold.
"""

from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from config.settings import settings
from data.market import AlpacaMarketData, Timeframe

log = logging.getLogger(__name__)

_CACHE_TTL_SECS = 300.0


class SPYProvider:
    """Thread-safe SPY daily-close provider with TTL cache."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cached_at: datetime | None = None
        self._cache: list[tuple[str, Decimal]] = []
        self._client: AlpacaMarketData | None = None
        if settings.alpaca_api_key and settings.alpaca_secret_key:
            try:
                self._client = AlpacaMarketData(
                    settings.alpaca_api_key, settings.alpaca_secret_key
                )
            except Exception:
                log.warning("SPYProvider: Alpaca client init failed", exc_info=True)

    def daily_closes(self, days: int = 60) -> list[tuple[str, Decimal]]:
        """Return list of (iso_date, close) for SPY over the past `days` days."""
        with self._lock:
            if self._fresh():
                return list(self._cache)
            if self._client is None:
                return []
            try:
                # Back off end by 20 min so free-tier IEX feed isn't blocked by
                # SIP-data restriction ("subscription does not permit querying recent SIP data").
                end = datetime.now(UTC) - timedelta(minutes=20)
                start = end - timedelta(days=days + 5)
                bars = self._client.get_bars("SPY", start, end, Timeframe.DAY)
                self._cache = [(b.timestamp.date().isoformat(), b.close) for b in bars]
                self._cached_at = datetime.now(UTC)
                return list(self._cache)
            except Exception:
                log.warning("SPYProvider: get_bars failed", exc_info=True)
                return list(self._cache)

    def _fresh(self) -> bool:
        if self._cached_at is None or not self._cache:
            return False
        return (datetime.now(UTC) - self._cached_at).total_seconds() < _CACHE_TTL_SECS
