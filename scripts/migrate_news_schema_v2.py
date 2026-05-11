"""Plan 2c T2.2 schema migration: add NewsScorer columns to news_items.

Adds (idempotent, safe to run twice):
  - impact            INTEGER (1-5; null until scored)
  - affected_symbols  TEXT    (CSV of symbols the score deemed affected)
  - surprise          TEXT    ("low" | "med" | "high"; null until scored)
  - scored_at         TIMESTAMP (ISO; null until scored)

The `scored_at` column is included now so the Tier 3 followup loop
(news score vs. actual price move) has its join key without needing
a second migration later. See logs/plan_2c_followups.md item #1.

Run from repo root with the bot stopped:

    uv run python -m scripts.migrate_news_schema_v2

Idempotent: each ADD COLUMN is wrapped in a try/except for SQLite's
"duplicate column name" error so reruns are no-ops.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_DB_PATH = Path("data/news.db")

_NEW_COLUMNS: tuple[tuple[str, str], ...] = (
    ("impact", "INTEGER"),
    ("affected_symbols", "TEXT"),
    ("surprise", "TEXT"),
    ("scored_at", "TIMESTAMP"),
)


def _add_column_idempotent(con: sqlite3.Connection, name: str, ddl_type: str) -> str:
    """Return 'added' or 'exists'; never raises for the duplicate-column case."""
    try:
        con.execute(f"ALTER TABLE news_items ADD COLUMN {name} {ddl_type}")
        return "added"
    except sqlite3.OperationalError as exc:
        if "duplicate column" in str(exc).lower():
            return "exists"
        raise


def main() -> int:
    if not _DB_PATH.exists():
        print(f"NOTE: {_DB_PATH} does not exist; nothing to migrate. "
              f"NewsStore will create the table fresh on first use.")
        return 0
    print(f"Migrating {_DB_PATH} -> news_items v2 (Plan 2c T2.2 schema)")
    with sqlite3.connect(str(_DB_PATH)) as con:
        for name, ddl_type in _NEW_COLUMNS:
            status = _add_column_idempotent(con, name, ddl_type)
            print(f"  {name:18s} {ddl_type:10s} -> {status}")
        con.commit()
    print("Migration complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
