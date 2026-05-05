"""One-shot migration: collapse dual-form crypto lot symbols.

Before this migration, LotLedger.open_lot persisted fill.symbol verbatim,
so a single logical crypto position could produce two lots — one under
"BTC/USD" (broker stream form) and one under "BTCUSD" (submitted form).
The fix in execution/lots.py normalizes new lots, but historical rows
still carry both forms.

Running this script:
    python -m ops.migrate_lot_symbols [path/to/lots.db]

Idempotent: rerunning is a no-op (no rows match the LIKE clause once
they've been normalized).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


def migrate(db_path: Path) -> int:
    """Return number of rows updated."""
    if not db_path.exists():
        print(f"no such file: {db_path}")
        return 0
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.execute(
            "SELECT id, symbol FROM lots WHERE symbol LIKE '%/%'"
        )
        rows = cur.fetchall()
        if not rows:
            print(f"{db_path}: no slashed-form lot symbols found — nothing to do")
            return 0

        for lot_id, sym in rows:
            new_sym = sym.replace("/", "")
            print(f"  {lot_id[:8]}…  {sym!r:>12} → {new_sym!r}")

        conn.execute(
            "UPDATE lots SET symbol = REPLACE(symbol, '/', '') "
            "WHERE symbol LIKE '%/%'"
        )
        conn.commit()
        print(f"{db_path}: migrated {len(rows)} lot(s)")
        return len(rows)
    finally:
        conn.close()


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/lots.db")
    n = migrate(target)
    sys.exit(0 if n >= 0 else 1)
