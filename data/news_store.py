"""SQLite-backed persistence for NewsItem objects.

Dedupes on URL. Indexes on (published_at, symbol) for fast recent-window
queries. Pruning is the caller's responsibility — call prune_older_than()
periodically to keep the table bounded.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.types import NewsItem, NewsSource


_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_items (
    url           TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    headline      TEXT NOT NULL,
    published_at  TEXT NOT NULL,
    summary       TEXT,
    sentiment     REAL,
    body          TEXT
);

CREATE TABLE IF NOT EXISTS news_symbols (
    url     TEXT NOT NULL,
    symbol  TEXT NOT NULL,
    PRIMARY KEY (url, symbol),
    FOREIGN KEY (url) REFERENCES news_items(url) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_news_published_at ON news_items(published_at);
CREATE INDEX IF NOT EXISTS ix_news_symbol ON news_symbols(symbol);
"""


class NewsStore:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path, timeout=10.0)
        con.execute("PRAGMA foreign_keys = ON")
        return con

    def add_items(self, items: list[NewsItem]) -> int:
        """Insert items, ignoring duplicates by URL. Returns rows newly inserted."""
        if not items:
            return 0
        inserted = 0
        with self._lock, self._connect() as con:
            for item in items:
                cur = con.execute(
                    "INSERT OR IGNORE INTO news_items "
                    "(url, source, headline, published_at, summary, sentiment, body) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.url,
                        item.source.value,
                        item.headline,
                        item.published_at.isoformat(),
                        item.summary,
                        item.sentiment,
                        item.body,
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
                for sym in item.symbols:
                    con.execute(
                        "INSERT OR IGNORE INTO news_symbols (url, symbol) VALUES (?, ?)",
                        (item.url, sym),
                    )
        return inserted

    def recent_for_symbols(
        self,
        symbols: list[str],
        since: datetime,
        limit: int = 50,
    ) -> list[NewsItem]:
        """Return items whose symbol overlap is in `symbols` and published since `since`.

        Sorted by published_at DESC. Each item appears once even if it matches
        multiple symbols.
        """
        if not symbols:
            return []
        placeholders = ",".join("?" * len(symbols))
        sql = (
            f"SELECT DISTINCT n.url, n.source, n.headline, n.published_at, "
            f"  n.summary, n.sentiment, n.body "
            f"FROM news_items n "
            f"JOIN news_symbols s ON n.url = s.url "
            f"WHERE s.symbol IN ({placeholders}) AND n.published_at >= ? "
            f"ORDER BY n.published_at DESC LIMIT ?"
        )
        params: list[object] = [*symbols, since.isoformat(), limit]
        with self._lock, self._connect() as con:
            rows = con.execute(sql, params).fetchall()
        items: list[NewsItem] = []
        for url, source, headline, published_at, summary, sentiment, body in rows:
            with self._lock, self._connect() as con:
                sym_rows = con.execute(
                    "SELECT symbol FROM news_symbols WHERE url = ?", (url,)
                ).fetchall()
            items.append(
                NewsItem(
                    source=NewsSource(source),
                    headline=headline,
                    url=url,
                    published_at=datetime.fromisoformat(published_at),
                    symbols=tuple(r[0] for r in sym_rows),
                    summary=summary,
                    sentiment=sentiment,
                    body=body,
                )
            )
        return items

    def prune_older_than(self, cutoff: datetime) -> int:
        """Delete items published before `cutoff`. Returns rows deleted."""
        with self._lock, self._connect() as con:
            cur = con.execute(
                "DELETE FROM news_items WHERE published_at < ?", (cutoff.isoformat(),)
            )
            return cur.rowcount

    def count(self) -> int:
        with self._lock, self._connect() as con:
            row = con.execute("SELECT COUNT(*) FROM news_items").fetchone()
            return int(row[0]) if row else 0


def default_retention_cutoff(now: datetime | None = None) -> datetime:
    """News older than 90 days is dropped — matches Opus deep-dive horizon."""
    now = now or datetime.now(UTC)
    return now - timedelta(days=90)
