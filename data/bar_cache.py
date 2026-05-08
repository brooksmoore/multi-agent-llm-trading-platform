"""SQLite cache for daily OHLCV bars.

Wraps a `MarketData` implementation: cache hits skip the upstream fetch,
cache misses fetch only the gap. The most recent UTC date is never cached
(intraday bars are not yet final).
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from data.market import Bar, MarketData, Timeframe

_log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bars_daily (
    symbol  TEXT NOT NULL,
    date    TEXT NOT NULL,           -- ISO date (YYYY-MM-DD)
    ts      TEXT NOT NULL,           -- full ISO timestamp from source
    open    TEXT NOT NULL,
    high    TEXT NOT NULL,
    low     TEXT NOT NULL,
    close   TEXT NOT NULL,
    volume  INTEGER NOT NULL,
    vwap    TEXT,
    PRIMARY KEY (symbol, date)
);
CREATE INDEX IF NOT EXISTS ix_bars_daily_symbol_date ON bars_daily(symbol, date);
"""


def _bar_date(b: Bar) -> str:
    return b.timestamp.date().isoformat()


class BarCache:
    """Thin sqlite layer; caller-managed lifecycle."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path, timeout=10.0)

    def read_range(
        self, symbols: list[str], start: datetime, end: datetime
    ) -> dict[str, list[Bar]]:
        if not symbols:
            return {}
        out: dict[str, list[Bar]] = {sym: [] for sym in symbols}
        start_d = start.date().isoformat()
        end_d = end.date().isoformat()
        placeholders = ",".join("?" * len(symbols))
        sql = (
            f"SELECT symbol, ts, open, high, low, close, volume, vwap "
            f"FROM bars_daily WHERE symbol IN ({placeholders}) "
            f"AND date >= ? AND date <= ? ORDER BY symbol, date ASC"
        )
        with self._lock, self._connect() as con:
            rows = con.execute(sql, [*symbols, start_d, end_d]).fetchall()
        for sym, ts_iso, o, h, lo, c, v, vw in rows:
            out[sym].append(
                Bar(
                    symbol=sym,
                    timestamp=datetime.fromisoformat(ts_iso),
                    open=Decimal(o),
                    high=Decimal(h),
                    low=Decimal(lo),
                    close=Decimal(c),
                    volume=int(v),
                    vwap=Decimal(vw) if vw is not None else None,
                )
            )
        return out

    def last_cached_date(self, symbols: list[str]) -> dict[str, str | None]:
        if not symbols:
            return {}
        out: dict[str, str | None] = {sym: None for sym in symbols}
        placeholders = ",".join("?" * len(symbols))
        sql = (
            f"SELECT symbol, MAX(date) FROM bars_daily "
            f"WHERE symbol IN ({placeholders}) GROUP BY symbol"
        )
        with self._lock, self._connect() as con:
            rows = con.execute(sql, list(symbols)).fetchall()
        for sym, max_date in rows:
            out[sym] = max_date
        return out

    def write(self, bars: list[Bar], *, skip_today: bool = True) -> int:
        """Upsert bars. By default, bars dated today (UTC) are skipped — they
        may still be intraday-mutable."""
        if not bars:
            return 0
        today = datetime.now(UTC).date().isoformat()
        rows = []
        for b in bars:
            d = _bar_date(b)
            if skip_today and d >= today:
                continue
            rows.append((
                b.symbol, d, b.timestamp.isoformat(),
                str(b.open), str(b.high), str(b.low), str(b.close),
                int(b.volume),
                str(b.vwap) if b.vwap is not None else None,
            ))
        if not rows:
            return 0
        with self._lock, self._connect() as con:
            con.executemany(
                "INSERT OR REPLACE INTO bars_daily "
                "(symbol, date, ts, open, high, low, close, volume, vwap) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        return len(rows)


class CachedMarketData:
    """MarketData wrapper that serves daily bars from a SQLite cache.

    Other methods delegate to the underlying instance unchanged.
    """

    def __init__(self, underlying: MarketData, cache: BarCache) -> None:
        self._md = underlying
        self._cache = cache

    # ── Cached path ───────────────────────────────────────────────────────────

    def get_bars_batch(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
        timeframe: Timeframe = Timeframe.DAY,
    ) -> dict[str, list[Bar]]:
        # Only the daily timeframe is cacheable here; everything else passes
        # through.
        if timeframe != Timeframe.DAY or not symbols:
            return self._md.get_bars_batch(symbols, start, end, timeframe)

        cached = self._cache.read_range(symbols, start, end)
        last_dates = self._cache.last_cached_date(symbols)

        # Determine the earliest date we still need from upstream. For each
        # symbol: if cache has a max date in [start, end-1], we need
        # [max+1day, end]; otherwise we need [start, end].
        today = datetime.now(UTC).date()
        end_d = end.date()
        gap_starts: list[datetime] = []
        for sym in symbols:
            last_iso = last_dates.get(sym)
            if last_iso is None:
                gap_starts.append(start)
                continue
            last_d = datetime.fromisoformat(last_iso).date()
            if last_d >= min(end_d, today - timedelta(days=1)):
                # Cache fully covers the historical (immutable) portion; we
                # still need today's live bar.
                gap_starts.append(datetime.combine(today, datetime.min.time(), UTC))
            else:
                gap_starts.append(
                    datetime.combine(last_d + timedelta(days=1), datetime.min.time(), UTC)
                )

        gap_start = min(gap_starts)
        if gap_start > end:
            return cached

        try:
            fresh = self._md.get_bars_batch(symbols, gap_start, end, timeframe)
        except Exception:
            _log.warning("upstream get_bars_batch failed; serving cache only", exc_info=True)
            return cached

        # Persist everything except today's (still-mutable) bars.
        all_fresh: list[Bar] = []
        for sym_bars in fresh.values():
            all_fresh.extend(sym_bars)
        self._cache.write(all_fresh, skip_today=True)

        # Merge cache + fresh, dedup by date, return only [start, end].
        merged: dict[str, list[Bar]] = {sym: [] for sym in symbols}
        start_d = start.date()
        for sym in symbols:
            seen_dates: dict[str, Bar] = {}
            for b in cached.get(sym, []):
                seen_dates[_bar_date(b)] = b
            for b in fresh.get(sym, []):
                seen_dates[_bar_date(b)] = b  # fresh wins
            bars = [
                b for d, b in seen_dates.items()
                if start_d <= datetime.fromisoformat(d).date() <= end_d
            ]
            bars.sort(key=lambda x: x.timestamp)
            merged[sym] = bars
        return merged

    # ── Pass-through ──────────────────────────────────────────────────────────

    def get_bars(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        timeframe: Timeframe = Timeframe.DAY,
    ) -> list[Bar]:
        return self.get_bars_batch([symbol], start, end, timeframe).get(symbol, [])

    def get_latest_bar(self, symbol: str) -> Bar | None:
        return self._md.get_latest_bar(symbol)

    def get_latest_quote(self, symbol: str):  # type: ignore[no-untyped-def]
        return self._md.get_latest_quote(symbol)

    def get_snapshots(self, symbols: list[str]) -> dict[str, Bar]:
        return self._md.get_snapshots(symbols)
