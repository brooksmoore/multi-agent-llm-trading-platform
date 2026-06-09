#!/usr/bin/env python
"""One-off: close phantom dust lots left by fractional crypto sells.

Fractional crypto sells can leave a sub-unit remainder (~1e-8) in a lot whose
`remaining_qty` never reached exactly zero, so `is_closed` stayed 0. These show
as phantom ~$0 open positions and spam the agent_state_tracker "missing marks"
warnings. The live close path already snaps sub-DUST_NOTIONAL_USD slivers to
zero (execution/lots.py), so this only cleans LEGACY dust created before that
logic existed.

Safe: only touches open lots whose remaining notional is below the threshold.
Prints what it will do and requires --apply to write. Stop the bot first to
avoid racing its writes (the bot holds an in-process lock, not a cross-process
one).

    .venv/bin/python scripts/clean_dust_lots.py            # dry-run (default)
    .venv/bin/python scripts/clean_dust_lots.py --apply    # write
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_DB = Path("data/lots.db")
_DUST_QTY = 1e-4  # any open lot with remaining_qty below this is dust


def main() -> int:
    apply = "--apply" in sys.argv
    if not _DB.exists():
        print(f"ERROR: {_DB} not found (run from repo root).")
        return 2

    con = sqlite3.connect(str(_DB))
    rows = con.execute(
        "SELECT id, agent_id, symbol, remaining_qty FROM lots "
        "WHERE is_closed = 0 AND CAST(remaining_qty AS REAL) > 0 "
        "AND CAST(remaining_qty AS REAL) < ?",
        (_DUST_QTY,),
    ).fetchall()

    if not rows:
        print("No dust lots found. Nothing to do.")
        return 0

    print(f"Found {len(rows)} dust lot(s):")
    for lot_id, agent, symbol, rq in rows:
        print(f"  {agent:8s} {symbol:8s} remaining_qty={rq}  (lot {lot_id[:8]}…)")

    if not apply:
        print("\nDry-run. Re-run with --apply to close these "
              "(set remaining_qty=0, is_closed=1).")
        con.close()
        return 0

    ids = [r[0] for r in rows]
    con.executemany(
        "UPDATE lots SET remaining_qty = '0', is_closed = 1 WHERE id = ?",
        [(i,) for i in ids],
    )
    con.commit()
    con.close()
    print(f"\nClosed {len(ids)} dust lot(s). Restart the bot to refresh its "
          "in-memory ledger.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
