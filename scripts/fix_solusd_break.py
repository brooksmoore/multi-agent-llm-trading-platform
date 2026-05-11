"""One-off: write off the in-kind Alpaca crypto fee on the SOLUSD lot.

Background: a buy of 19.999849 SOLUSD on 2026-05-06 12:00:15 UTC delivered
19.9498 SOL net of Alpaca's 0.25% in-kind taker fee. OMS recorded the gross
qty, so books and broker disagreed by 0.05 SOL ($4.45) every reconcile tick.

This script books a synthetic SELL fill of exactly that fee qty so the OMS
event log, the lot ledger, and the broker position all agree. Idempotent
side-effects: writes one ORDER_SUBMIT_INTENT, one ORDER_ACCEPTED, and one
FILL_RECEIVED event to data/oms.db, and closes 0.05 SOLUSD from the open
lot in data/lots.db.

Run with the bot stopped:

    uv run python -m scripts.fix_solusd_break
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from core.events import EventBus
from core.types import AgentId
from execution.lots import LotLedger
from execution.oms import OMS, OMSStore
from execution.broker import Broker, BrokerAccount, BrokerOrderStatus, BrokerPosition

# Drift recovered from logs/app.log:
#   "expected=19.9998 broker=19.9498 (qty_drift=0.0500 dollar_drift=$4.45)"
SYMBOL = "SOLUSD"
FEE_QTY = Decimal("0.050")
FEE_PRICE = Decimal("89.2331")  # buy fill price; cost basis stays consistent
AGENT = AgentId.HAIKU
TS = datetime(2026, 5, 6, 12, 0, 15, tzinfo=UTC)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class _NullBroker(Broker):
    """OMS only needs `register_event_callback` for this offline script."""

    def submit_order(self, order):  # type: ignore[override]
        raise NotImplementedError

    def cancel_order(self, broker_order_id: str) -> None:  # type: ignore[override]
        raise NotImplementedError

    def get_order(self, broker_order_id: str) -> BrokerOrderStatus | None:  # type: ignore[override]
        return None

    def list_positions(self) -> list[BrokerPosition]:  # type: ignore[override]
        return []

    def get_account(self) -> BrokerAccount:  # type: ignore[override]
        raise NotImplementedError

    def register_event_callback(self, callback) -> None:  # type: ignore[override]
        return None

    def start_stream(self) -> None: return None  # type: ignore[override]
    def stop_stream(self) -> None: return None  # type: ignore[override]


def main() -> None:
    bus = EventBus()
    lots = LotLedger(db_path=str(DATA_DIR / "lots.db"))
    store = OMSStore(str(DATA_DIR / "oms.db"))
    # Skip OMS.recover() — book_fee_offset only needs to append three events
    # to the log (submit_intent / accepted / fill_received). On the next bot
    # startup, the regular recover() replay will see the ghost SELL and
    # _compute_expected_positions will net it against the original buys.
    oms = OMS(_NullBroker(), store, bus)

    # Wire lot ledger to fill events so the SELL closes 0.05 SOL via FIFO.
    from core.events import FillReceivedEvent

    def _on_fill(ev: object) -> None:
        if isinstance(ev, FillReceivedEvent):
            lots.book_fill(ev.fill)

    bus.subscribe("fill.received", _on_fill)

    before_lots = lots.open_lots(AGENT, SYMBOL)
    print(f"Before: open {SYMBOL} lots = "
          f"{[(str(l.qty), str(l.remaining_qty)) for l in before_lots]}")

    oms.book_fee_offset(
        symbol=SYMBOL,
        qty=FEE_QTY,
        price=FEE_PRICE,
        agent_id=AGENT,
        ts=TS,
    )

    after_lots = lots.open_lots(AGENT, SYMBOL)
    print(f"After:  open {SYMBOL} lots = "
          f"{[(str(l.qty), str(l.remaining_qty)) for l in after_lots]}")
    print(f"Total remaining qty: "
          f"{sum((l.remaining_qty for l in after_lots), Decimal('0'))}")


if __name__ == "__main__":
    main()
