"""Bridges fill→lot-close→intent→conviction so the CalibrationTracker
gets win/loss/flat outcomes attributed to the *opening* intent.

Without this, calibration.db stays empty and Brier scores show 0.00 forever
because nothing in production calls `CalibrationTracker.record()`.

Pipeline on every SELL fill:
  1. After lots.book_fill, query LotLedger for lots whose exit_fill_id
     matches this fill — those are the lots just (fully) closed.
  2. For each closed lot, walk OMS events for the *entry* fill's order
     to find the opening submit_intent and its conviction.
  3. Classify the lot's realized return as win/loss/flat (sign of pct
     return, with a small dead-band around 0).
  4. Call CalibrationTracker.record(intent_id, agent_id, conviction, outcome).

Partial closes don't trigger here — only fully-closed lots have an
exit_fill_id set. That's intentional: Brier needs a terminal outcome.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING

from core.events import EventBus, FillReceivedEvent
from core.types import OrderSide

if TYPE_CHECKING:
    from agents.calibration import CalibrationTracker
    from execution.lots import Lot, LotLedger
    from execution.oms_store import OMSStore

log = logging.getLogger(__name__)

# Pct return dead-band that classifies a closed lot as "flat" rather than
# win/loss. 0.5% accounts for round-trip slippage on fractional retail fills.
_FLAT_DEADBAND_PCT: Decimal = Decimal("0.005")


class CalibrationRecorder:
    """Subscribe to fill events; record terminal lot outcomes to CalibrationTracker."""

    def __init__(
        self,
        calibration: CalibrationTracker,
        lots: LotLedger,
        oms_store: OMSStore,
        bus: EventBus,
    ) -> None:
        self._cal = calibration
        self._lots = lots
        self._store = oms_store
        bus.subscribe("fill.received", self._on_fill)

    def _on_fill(self, event: FillReceivedEvent) -> None:
        fill = event.fill
        if fill.side != OrderSide.SELL:
            return
        # The LotLedger writes exit_fill_id when a lot is fully closed by this
        # fill. Querying *after* book_fill (which app.py runs first) yields the
        # lots that just terminalized.
        try:
            closed_lots = self._lots.lots_by_exit_fill(fill.id)
        except Exception:
            log.exception("calibration: lots_by_exit_fill failed for %s", fill.id)
            return
        for lot in closed_lots:
            try:
                self._record_for_lot(lot)
            except Exception:
                log.exception("calibration: record failed for lot %s", lot.id)

    def _record_for_lot(self, lot: Lot) -> None:
        if lot.exit_price is None or lot.entry_price <= Decimal("0"):
            return
        # Realized return on the closed quantity.
        pct = (lot.exit_price - lot.entry_price) / lot.entry_price
        if pct > _FLAT_DEADBAND_PCT:
            outcome = "win"
        elif pct < -_FLAT_DEADBAND_PCT:
            outcome = "loss"
        else:
            outcome = "flat"

        opening = self._lookup_opening_intent(lot.entry_fill_id)
        if opening is None:
            return
        intent_id, conviction = opening
        self._cal.record(
            intent_id=intent_id,
            agent_id=str(lot.agent_id).split(".")[-1].lower(),
            conviction=conviction,
            outcome=outcome,
        )

    def _lookup_opening_intent(self, entry_fill_id: object) -> tuple[str, int] | None:
        """Walk the order's OMS events to find submit_intent → (intent_id, conviction).

        Lots only carry the *fill* id; the intent is reachable via
        order_id (fill→order→submit_intent payload).
        """
        # Find the fill.received event for this entry_fill_id to get its order_id.
        order_id = None
        for ev in self._store.iter_all():
            if ev.kind.value != "fill.received":
                continue
            if str(ev.payload.get("fill_id") or ev.payload.get("id")) == str(entry_fill_id):
                order_id = ev.order_id
                break
        if order_id is None:
            return None

        intent_id: str = ""
        conviction: int = 0
        for ev in self._store.iter_for_order(order_id):
            if ev.kind.value != "order.submit_intent":
                continue
            raw_iid = ev.payload.get("intent_id")
            if isinstance(raw_iid, dict):
                intent_id = str(raw_iid.get("__uuid__", ""))
            else:
                intent_id = str(raw_iid or "")
            intent = ev.payload.get("intent") or {}
            try:
                conviction = int(intent.get("conviction") or 0)
            except (TypeError, ValueError):
                conviction = 0
            break
        if not intent_id or conviction <= 0:
            return None
        return intent_id, conviction
