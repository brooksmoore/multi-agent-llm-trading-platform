"""Inject ghost SELL fills into the OMS event log after a manual flatten.

flatten_alpaca_paper.py closes positions directly via the Alpaca API, bypassing
the broker websocket callback path. The OMS never receives the fill events, so
_compute_expected_positions() still sees a net long for each flattened symbol.
On restart the reconciler trips the kill switch immediately.

This script:
  1. Reads all fill.received events from oms.db to compute net OMS position per symbol.
  2. For each symbol with a net long > 0, looks up the exit price from lots.db
     (written by flatten_alpaca_paper.py). Falls back to Alpaca's last trade price.
  3. Appends ghost order.submit_intent + order.accepted + fill.received (SELL)
     events that exactly cancel the net long.
  4. On restart, OMS recovery replays these events: net positions become 0,
     reconciler finds no mismatch, kill switch stays clear.

Run with the bot STOPPED. Dry run by default.

    uv run python -m scripts.reconcile_oms_after_flatten            # dry run
    uv run python -m scripts.reconcile_oms_after_flatten --execute  # apply
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from alpaca.trading.client import TradingClient

from execution.oms_store import dumps


OMS_DB = Path("data/oms.db")
LOTS_DB = Path("data/lots.db")

_AGENT_ID = "manager"
_GHOST_BROKER_PREFIX = "ghost-flatten-"
_QTY_THRESHOLD = Decimal("1e-6")


def _compute_net_positions(conn: sqlite3.Connection) -> dict[str, Decimal]:
    """Sum fill.received events → net position per symbol (longs only)."""
    import json

    rows = conn.execute(
        "SELECT payload FROM oms_events WHERE kind = 'fill.received'"
    ).fetchall()
    net: dict[str, Decimal] = {}
    for (raw,) in rows:
        p = json.loads(raw, object_hook=_decode_hook)
        sym = p["symbol"]
        qty = p["qty"]
        side = p["side"]  # "buy" or "sell" (StrEnum serialises to its value)
        sign = Decimal("1") if side == "buy" else Decimal("-1")
        net[sym] = net.get(sym, Decimal("0")) + sign * qty
    return {s: q for s, q in net.items() if q > _QTY_THRESHOLD}


def _decode_hook(obj: dict) -> object:
    if "__decimal__" in obj:
        return Decimal(obj["__decimal__"])
    if "__datetime__" in obj:
        return datetime.fromisoformat(obj["__datetime__"])
    if "__uuid__" in obj:
        return uuid.UUID(obj["__uuid__"])
    return obj


def _lots_exit_prices(lots_conn: sqlite3.Connection) -> dict[str, Decimal]:
    """Most recent exit price per symbol from lots.db."""
    rows = lots_conn.execute(
        "SELECT symbol, exit_price FROM lots WHERE is_closed=1 AND exit_price IS NOT NULL"
        " ORDER BY exit_date DESC"
    ).fetchall()
    prices: dict[str, Decimal] = {}
    for sym, price in rows:
        sym = sym.upper()
        if sym not in prices:
            prices[sym] = Decimal(str(price))
    return prices


def _alpaca_last_prices(symbols: list[str], client: TradingClient) -> dict[str, Decimal]:
    """Fetch last trade price for each symbol from Alpaca as fallback."""
    prices: dict[str, Decimal] = {}
    try:
        from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
        from alpaca.data.requests import StockLatestTradeRequest, CryptoLatestTradeRequest

        api_key = os.environ.get("ALPACA_API_KEY", "")
        secret = os.environ.get("ALPACA_SECRET_KEY", "")
        crypto_syms = [s for s in symbols if s.endswith("USD")]
        equity_syms = [s for s in symbols if not s.endswith("USD")]

        if equity_syms:
            sc = StockHistoricalDataClient(api_key, secret)
            trades = sc.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=equity_syms))
            for sym, t in trades.items():
                prices[sym.upper()] = Decimal(str(t.price))
        if crypto_syms:
            cc = CryptoHistoricalDataClient(api_key, secret)
            trades = cc.get_crypto_latest_trade(CryptoLatestTradeRequest(symbol_or_symbols=crypto_syms))
            for sym, t in trades.items():
                prices[sym.upper()] = Decimal(str(t.price))
    except Exception as exc:
        print(f"  WARN: could not fetch Alpaca prices ({exc}); affected symbols will use 0")
    return prices


def _ghost_events(
    symbol: str,
    net_qty: Decimal,
    price: Decimal,
    ts: datetime,
) -> list[tuple[str, str, str]]:
    """Build (kind, order_id_str, payload_json) for the 3 ghost events."""
    order_id = uuid.uuid4()
    broker_id = f"ghost-flatten-{order_id}"
    fill_id = uuid.uuid4()
    intent_id = uuid.uuid4()

    order_payload = {
        "id": order_id,
        "intent_id": intent_id,
        "agent_id": _AGENT_ID,
        "symbol": symbol,
        "side": "sell",
        "qty": net_qty,
        "order_type": "market",
        "order_class": "simple",
        "time_in_force": "gtc",
        "state": "pending",
        "created_at": ts,
        "limit_price": None,
        "stop_price": None,
        "is_letf": False,
    }
    accepted_payload = {"broker_order_id": broker_id}
    fill_payload = {
        "id": fill_id,
        "order_id": order_id,
        "agent_id": _AGENT_ID,
        "symbol": symbol,
        "side": "sell",
        "qty": net_qty,
        "price": price,
        "timestamp": ts,
        "commission": Decimal("0"),
        "is_partial": False,
    }
    oid = str(order_id)
    return [
        ("order.submit_intent", oid, dumps(order_payload)),
        ("order.accepted",      oid, dumps(accepted_payload)),
        ("fill.received",       oid, dumps(fill_payload)),
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true",
                        help="Actually write events. Without this flag: dry run.")
    args = parser.parse_args()

    api_key = os.environ.get("ALPACA_API_KEY")
    secret = os.environ.get("ALPACA_SECRET_KEY")
    if not api_key or not secret:
        print("ERROR: ALPACA_API_KEY / ALPACA_SECRET_KEY not set.")
        return 1

    client = TradingClient(api_key, secret, paper=True)

    oms_conn = sqlite3.connect(str(OMS_DB))
    lots_conn = sqlite3.connect(str(LOTS_DB))

    # Rollback any prior ghost-flatten events from a previous run (idempotent).
    # Match on order_ids that have an order.accepted event whose payload contains
    # our ghost broker prefix.
    poison_ids = [
        oid for (oid,) in oms_conn.execute(
            "SELECT DISTINCT order_id FROM oms_events "
            "WHERE kind='order.accepted' AND payload LIKE ?",
            (f'%"{_GHOST_BROKER_PREFIX}%',),
        ).fetchall()
    ]
    if poison_ids:
        placeholders = ",".join("?" * len(poison_ids))
        n_del = oms_conn.execute(
            f"DELETE FROM oms_events WHERE order_id IN ({placeholders})",
            poison_ids,
        ).rowcount
        oms_conn.commit()
        print(f"Rolled back {n_del} prior ghost-flatten events ({len(poison_ids)} order_ids).\n")

    net = _compute_net_positions(oms_conn)
    if not net:
        print("OMS already flat — no ghost fills needed.")
        return 0

    print(f"OMS net long positions to zero out ({len(net)} symbols):")
    for sym, qty in sorted(net.items()):
        print(f"  {sym:<10} qty={qty}")

    lot_prices = _lots_exit_prices(lots_conn)
    missing = [s for s in net if s not in lot_prices]
    if missing:
        print(f"\nFetching Alpaca last-trade prices for {len(missing)} symbols not in lots.db...")
        alpaca_prices = _alpaca_last_prices(missing, client)
        lot_prices.update(alpaca_prices)

    ts = datetime.now(UTC)

    print("\nGhost events to inject:")
    all_events: list[tuple[str, str, str]] = []
    for sym, qty in sorted(net.items()):
        price = lot_prices.get(sym, Decimal("0"))
        events = _ghost_events(sym, qty, price, ts)
        all_events.extend(events)
        print(f"  {sym:<10} SELL qty={qty:.6f} @ ${price} → {len(events)} events")

    if not args.execute:
        print(f"\n*** DRY RUN — {len(all_events)} events would be written. Pass --execute to apply. ***")
        return 0

    print(f"\n*** EXECUTE — writing {len(all_events)} events to {OMS_DB} ***")
    ts_iso = ts.isoformat()
    with oms_conn:
        oms_conn.executemany(
            "INSERT INTO oms_events (ts, kind, order_id, payload) VALUES (?, ?, ?, ?)",
            [(ts_iso, kind, oid, payload) for kind, oid, payload in all_events],
        )
    print(f"Done. Restart the bot — reconciler should find 0 mismatches on first tick.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
