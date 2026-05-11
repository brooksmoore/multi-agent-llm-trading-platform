"""Per-sleeve P&L attribution from the OMS fill log + LotLedger + latest-bar marks.

T1.5 / Plan 2c. The capital reallocation engine, adversarial critique
pipeline, and dashboard all need a per-agent P&L view that the broker
account cannot provide (the broker reports one aggregate position
across sleeves). This module computes:

- realized P&L per agent by FIFO-matching SELL fills against prior BUY
  fills for the same (agent, symbol). Computed from the OMS fill log
  rather than the LotLedger because the ledger discards the per-sale
  price on partial exits — it only records `exit_price` when a lot is
  fully closed. Walking fills sidesteps that loss.
- unrealized P&L per agent from open lots, marked to each symbol's
  latest bar close.
- open / closed lot counts from the LotLedger.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from decimal import Decimal

from core.types import AgentId, Lot, OrderSide
from data.market import MarketData
from execution.lots import LotLedger
from execution.oms import _deserialize_fill
from execution.oms_store import EventKind, OMSStore

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PnLBreakdown:
    """Daily P&L snapshot for one agent."""

    realized: Decimal
    unrealized: Decimal
    total: Decimal
    num_open_lots: int
    num_closed_lots: int


def _latest_close_cache(
    market_data: MarketData, symbols: set[str]
) -> dict[str, Decimal]:
    """Resolve latest bar close per symbol, tolerant of per-symbol failures."""
    out: dict[str, Decimal] = {}
    for sym in symbols:
        try:
            bar = market_data.get_latest_bar(sym)
        except Exception:
            log.warning("attribution: get_latest_bar(%s) failed", sym, exc_info=True)
            bar = None
        if bar is not None:
            out[sym] = bar.close
    return out


def _lot_unrealized(lot: Lot, mark: Decimal | None) -> Decimal:
    """Unrealized P&L on the still-open portion of one lot. 0 if closed or no mark."""
    if lot.is_closed or lot.remaining_qty <= Decimal("0") or mark is None:
        return Decimal("0")
    return (mark - lot.entry_price) * lot.remaining_qty


def _realized_by_agent_from_fills(oms_store: OMSStore) -> dict[AgentId, Decimal]:
    """FIFO-match SELL fills against prior BUYs to compute per-agent realized P&L.

    Walks every FILL_RECEIVED event in seq order. For each SELL fill, consumes
    against the front of that (agent, symbol)'s BUY queue at the BUY's price;
    realized = (sell_price - buy_price) * matched_qty. Same FIFO discipline
    the LotLedger uses internally, so the totals reconcile.
    """
    queues: dict[tuple[AgentId, str], deque[list[Decimal]]] = {}
    realized: dict[AgentId, Decimal] = {}

    try:
        events = list(oms_store.recent_by_kind(EventKind.FILL_RECEIVED, n=1_000_000))
    except Exception:
        log.exception("attribution: oms_store.recent_by_kind(FILL_RECEIVED) failed")
        return {}
    # recent_by_kind is newest-first; we need oldest-first to FIFO correctly.
    events.reverse()

    for ev in events:
        try:
            fill = _deserialize_fill(ev.payload)
        except Exception:
            log.warning("attribution: skip undeserializable fill payload", exc_info=True)
            continue

        key = (fill.agent_id, fill.symbol)
        if fill.side == OrderSide.BUY:
            queues.setdefault(key, deque()).append([fill.qty, fill.price])
            continue

        # SELL: consume from the BUY queue.
        remaining_to_match = fill.qty
        q = queues.get(key)
        while remaining_to_match > Decimal("0") and q:
            front = q[0]  # [qty_remaining, price]
            consume = min(front[0], remaining_to_match)
            realized[fill.agent_id] = (
                realized.get(fill.agent_id, Decimal("0"))
                + (fill.price - front[1]) * consume
            )
            front[0] -= consume
            remaining_to_match -= consume
            if front[0] <= Decimal("0"):
                q.popleft()
        # If remaining_to_match > 0 here, the SELL exceeded recorded BUYs —
        # likely fills predate the OMS log (legacy data). Silently skip the
        # unmatched portion; the result is still correct for the matched part.

    return realized


def compute_daily_pnl(
    lots: LotLedger,
    oms_store: OMSStore,
    market_data: MarketData,
) -> dict[AgentId, PnLBreakdown]:
    """Aggregate per-agent realized + unrealized P&L.

    Realized comes from FIFO-matched fills (see `_realized_by_agent_from_fills`).
    Unrealized marks the remaining-open portion of every lot to its symbol's
    latest bar close. Symbols whose latest bar is unavailable contribute zero
    unrealized (logged once per symbol). Always returns one entry per sleeve
    AgentId — agents with no activity show all zeros so downstream consumers
    can rely on a stable shape.
    """
    realized_by_agent = _realized_by_agent_from_fills(oms_store)

    all_lots = lots.all_lots()
    symbols = {
        lot.symbol for lot in all_lots
        if not lot.is_closed and lot.remaining_qty > 0
    }
    marks = _latest_close_cache(market_data, symbols)

    accum: dict[AgentId, dict[str, Decimal | int]] = {
        aid: {
            "realized": realized_by_agent.get(aid, Decimal("0")),
            "unrealized": Decimal("0"),
            "num_open_lots": 0,
            "num_closed_lots": 0,
        }
        for aid in (AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS)
    }

    for lot in all_lots:
        if lot.agent_id not in accum:
            # Unknown agent (e.g. AgentId.MANAGER, which has no sleeve).
            continue
        bucket = accum[lot.agent_id]
        bucket["unrealized"] = bucket["unrealized"] + _lot_unrealized(  # type: ignore[operator]
            lot, marks.get(lot.symbol)
        )
        if lot.is_closed:
            bucket["num_closed_lots"] = int(bucket["num_closed_lots"]) + 1
        else:
            bucket["num_open_lots"] = int(bucket["num_open_lots"]) + 1

    out: dict[AgentId, PnLBreakdown] = {}
    for aid, b in accum.items():
        realized = b["realized"]
        unrealized = b["unrealized"]
        assert isinstance(realized, Decimal)
        assert isinstance(unrealized, Decimal)
        out[aid] = PnLBreakdown(
            realized=realized,
            unrealized=unrealized,
            total=realized + unrealized,
            num_open_lots=int(b["num_open_lots"]),
            num_closed_lots=int(b["num_closed_lots"]),
        )
    return out
