"""DuckDB-backed store for OHLCV bars and news items."""
from __future__ import annotations

import json
import threading
from datetime import datetime
from decimal import Decimal

import duckdb

from core.types import NewsItem, NewsSource
from data.market import Bar

_CREATE_BARS = """
CREATE TABLE IF NOT EXISTS bars (
    symbol      VARCHAR NOT NULL,
    timestamp   TIMESTAMPTZ NOT NULL,
    open        DECIMAL NOT NULL,
    high        DECIMAL NOT NULL,
    low         DECIMAL NOT NULL,
    close       DECIMAL NOT NULL,
    volume      BIGINT NOT NULL,
    vwap        DECIMAL,
    PRIMARY KEY (symbol, timestamp)
)
"""

_CREATE_NEWS = """
CREATE TABLE IF NOT EXISTS news (
    url          VARCHAR PRIMARY KEY,
    source       VARCHAR NOT NULL,
    headline     VARCHAR NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    symbols      VARCHAR NOT NULL,
    summary      VARCHAR,
    sentiment    DOUBLE,
    body         VARCHAR
)
"""

_UPSERT_BAR = """
INSERT INTO bars (symbol, timestamp, open, high, low, close, volume, vwap)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (symbol, timestamp) DO UPDATE SET
    open   = excluded.open,
    high   = excluded.high,
    low    = excluded.low,
    close  = excluded.close,
    volume = excluded.volume,
    vwap   = excluded.vwap
"""

_UPSERT_NEWS = """
INSERT INTO news (url, source, headline, published_at, symbols, summary, sentiment, body)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (url) DO UPDATE SET
    source       = excluded.source,
    headline     = excluded.headline,
    published_at = excluded.published_at,
    symbols      = excluded.symbols,
    summary      = excluded.summary,
    sentiment    = excluded.sentiment,
    body         = excluded.body
"""

_LOAD_BARS = """
SELECT symbol, timestamp, open, high, low, close, volume, vwap
FROM bars
WHERE symbol = ? AND timestamp >= ? AND timestamp <= ?
ORDER BY timestamp ASC
"""

_LOAD_NEWS = """
SELECT url, source, headline, published_at, symbols, summary, sentiment, body
FROM news
WHERE published_at >= ? AND published_at <= ?
ORDER BY published_at ASC
"""


class DataStore:
    def __init__(self, db_path: str = ":memory:") -> None:
        self._conn = duckdb.connect(db_path)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(_CREATE_BARS)
            self._conn.execute(_CREATE_NEWS)

    def save_bars(self, bars: list[Bar]) -> None:
        rows = [
            (
                b.symbol,
                b.timestamp,
                str(b.open),
                str(b.high),
                str(b.low),
                str(b.close),
                b.volume,
                str(b.vwap) if b.vwap is not None else None,
            )
            for b in bars
        ]
        with self._lock:
            self._conn.executemany(_UPSERT_BAR, rows)

    def load_bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        with self._lock:
            rows = self._conn.execute(_LOAD_BARS, [symbol, start, end]).fetchall()
        result: list[Bar] = []
        for row in rows:
            result.append(
                Bar(
                    symbol=str(row[0]),
                    timestamp=row[1],
                    open=Decimal(str(row[2])),
                    high=Decimal(str(row[3])),
                    low=Decimal(str(row[4])),
                    close=Decimal(str(row[5])),
                    volume=int(row[6]),
                    vwap=Decimal(str(row[7])) if row[7] is not None else None,
                )
            )
        return result

    def save_news(self, items: list[NewsItem]) -> None:
        rows = [
            (
                item.url,
                str(item.source),
                item.headline,
                item.published_at,
                json.dumps(list(item.symbols)),
                item.summary,
                item.sentiment,
                item.body,
            )
            for item in items
        ]
        with self._lock:
            self._conn.executemany(_UPSERT_NEWS, rows)

    def load_news(self, symbol: str, start: datetime, end: datetime) -> list[NewsItem]:
        with self._lock:
            rows = self._conn.execute(_LOAD_NEWS, [start, end]).fetchall()
        result: list[NewsItem] = []
        for row in rows:
            raw_symbols: list[str] = json.loads(str(row[4]))
            if symbol not in raw_symbols:
                continue
            result.append(
                NewsItem(
                    source=NewsSource(str(row[1])),
                    headline=str(row[2]),
                    url=str(row[0]),
                    published_at=row[3],
                    symbols=tuple(raw_symbols),
                    summary=str(row[5]) if row[5] is not None else None,
                    sentiment=float(row[6]) if row[6] is not None else None,
                    body=str(row[7]) if row[7] is not None else None,
                )
            )
        return result

    def close(self) -> None:
        with self._lock:
            self._conn.close()
