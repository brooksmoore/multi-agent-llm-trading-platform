"""Emergency flatten: liquidate every open position on the Alpaca paper account.

Used to recover from the planner over-leverage bug surfaced 2026-05-11:
the bot accumulated ~$186K of long exposure on a $102K paper account
because `Action.REBALANCE_TO` didn't compute deltas against existing
positions, and `Action.SELL` with target_weight=0 didn't route to
the close-position path. Both issues are pre-existing planner bugs;
see the followup fix on branch `planner-rebalance-delta`.

Run with the bot stopped, during US market hours for equity fills:

    uv run python -m scripts.flatten_alpaca_paper            # DRY RUN
    uv run python -m scripts.flatten_alpaca_paper --execute  # actually flatten

After execution, the script:
  1. Cancels every open Alpaca order.
  2. Calls Alpaca's close_all_positions(cancel_orders=True).
  3. Polls broker positions until all closed (or 90s timeout).
  4. Reconciles the local LotLedger: every open lot for any agent gets
     marked closed at the broker's fill price. The reconciler in the bot
     will pick up the new clean state on next start.

Crypto positions liquidate 24/7; equities require an open session.
If you run this outside market hours, the equity sells will be queued
as DAY orders and fill at next open.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from alpaca.trading.client import TradingClient


def _print_account(client: TradingClient, label: str) -> None:
    acct = client.get_account()
    print(f"\n=== Account ({label}) ===")
    print(f"  equity            ${acct.equity}")
    print(f"  cash              ${acct.cash}")
    print(f"  buying_power      ${acct.buying_power}")
    print(f"  long_market_val   ${acct.long_market_value}")


def _print_positions(client: TradingClient, label: str) -> int:
    positions = client.get_all_positions()
    print(f"\n=== Open positions ({label}, {len(positions)} symbols) ===")
    if not positions:
        print("  (none)")
        return 0
    print(f"  {'symbol':<10s} {'qty':>14s} {'market_val':>12s} {'unrealized_pl':>14s}")
    for p in sorted(positions, key=lambda x: abs(float(x.market_value or 0)), reverse=True):
        print(f"  {p.symbol:<10s} {str(p.qty):>14s} {str(p.market_value):>12s} {str(p.unrealized_pl):>14s}")
    return len(positions)


def _cancel_all_orders(client: TradingClient) -> int:
    """Cancel every open order. Returns count cancelled."""
    orders = client.get_orders()
    open_orders = [o for o in orders if str(o.status) not in ("OrderStatus.FILLED", "OrderStatus.CANCELED", "OrderStatus.EXPIRED")]
    print(f"\n=== Cancelling {len(open_orders)} open orders ===")
    for o in open_orders:
        try:
            client.cancel_order_by_id(o.id)
            print(f"  cancel {o.symbol} {o.side} {o.qty} → ok")
        except Exception as exc:
            print(f"  cancel {o.symbol} → FAILED: {exc}")
    return len(open_orders)


def _flatten_local_lots(db_path: Path, broker_fill_prices: dict[str, Decimal]) -> int:
    """Mark every open lot in the local LotLedger as closed.

    Uses the latest broker close price as the exit_price. Sets
    remaining_qty=0, is_closed=1, exit_date=today, exit_price=mark.
    Returns count of lots updated.
    """
    today = datetime.now(UTC).date().isoformat()
    n = 0
    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute(
            "SELECT id, symbol, remaining_qty FROM lots WHERE is_closed=0"
        ).fetchall()
        for lot_id, symbol, remaining in rows:
            mark = broker_fill_prices.get(symbol.upper())
            if mark is None:
                # Symbol has no broker mark (e.g. fully closed and missing from
                # final position list). Use the entry price as a fallback —
                # zero realized P&L is the safest guess.
                cur = conn.execute(
                    "SELECT entry_price FROM lots WHERE id=?", (lot_id,)
                ).fetchone()
                mark = Decimal(str(cur[0])) if cur else Decimal("0")
            conn.execute(
                "UPDATE lots SET remaining_qty='0', is_closed=1, "
                "exit_date=?, exit_price=? WHERE id=?",
                (today, str(mark), lot_id),
            )
            n += 1
        conn.commit()
    return n


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true",
                        help="Actually flatten. Without this flag the script is a dry run.")
    parser.add_argument("--poll-timeout", type=int, default=90,
                        help="Seconds to wait for positions to clear after close_all.")
    args = parser.parse_args()

    api_key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret:
        print("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set.")
        return 1
    client = TradingClient(api_key, secret, paper=True)

    _print_account(client, "before")
    n_before = _print_positions(client, "before")

    if n_before == 0:
        print("\nAlready flat. Exiting.")
        return 0

    if not args.execute:
        print("\n*** DRY RUN — pass --execute to actually flatten. ***")
        return 0

    print("\n*** EXECUTE MODE ***")
    print("Confirming intent to liquidate every open position on the Alpaca PAPER account.")
    print("Type 'yes' to proceed: ", end="", flush=True)
    response = sys.stdin.readline().strip().lower()
    if response != "yes":
        print("Aborted.")
        return 1

    _cancel_all_orders(client)
    print("\n=== Calling close_all_positions(cancel_orders=True) ===")
    closed = client.close_all_positions(cancel_orders=True)
    print(f"  submitted close orders for {len(closed)} positions")

    print(f"\n=== Polling for flat (up to {args.poll_timeout}s) ===")
    deadline = time.time() + args.poll_timeout
    while time.time() < deadline:
        remaining = client.get_all_positions()
        if not remaining:
            print("  → ALL CLEAR (broker reports 0 open positions)")
            break
        print(f"  {len(remaining)} positions still open, waiting...")
        time.sleep(5)
    else:
        print(f"  WARN: still have positions after {args.poll_timeout}s — check Alpaca dashboard")

    # Capture final marks for the lot-ledger update.
    final_positions = client.get_all_positions()
    broker_marks: dict[str, Decimal] = {}
    for p in final_positions:
        broker_marks[p.symbol.upper()] = Decimal(str(p.current_price or 0))

    print("\n=== Reconciling local LotLedger ===")
    lots_db = Path("data/lots.db")
    if not lots_db.exists():
        print(f"  WARN: {lots_db} not found; skip ledger update")
    else:
        # Backup before mutating
        backup = lots_db.with_suffix(".db.bak-pre-flatten-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
        backup.write_bytes(lots_db.read_bytes())
        print(f"  ledger backup: {backup}")
        n_updated = _flatten_local_lots(lots_db, broker_marks)
        print(f"  marked {n_updated} lots as closed in {lots_db}")

    _print_account(client, "after")
    _print_positions(client, "after")
    return 0


if __name__ == "__main__":
    sys.exit(main())
