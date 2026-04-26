"""Periodic reconciler: compares broker state against OMS state.

Runs every `interval_secs` (default 60) in a background daemon thread.

Two checks per cycle:
  1. Order reconciliation  — for each open OMS order, fetch broker status
     and invoke the OMS broker-event callback if status changed.
  2. Position reconciliation — compute expected positions from OMS fills
     and compare against broker positions. If any symbol deviates by
     >= `qty_tolerance` shares, trip the KillSwitchEngine reconciliation break.

Blueprint Principle 4: "Broker is the source of truth. A 1-share or $1 mismatch
flips the system to RECONCILIATION_BREAK."
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from core.types import Order, OrderSide, OrderState
from execution.broker import Broker, BrokerOrderEvent, BrokerOrderState, BrokerPosition
from execution.kill_switch import KillSwitchEngine
from execution.oms import OMS

logger = logging.getLogger(__name__)

_OPEN_STATES = frozenset({
    OrderState.PENDING,
    OrderState.SUBMITTED,
    OrderState.ACCEPTED,
    OrderState.PARTIAL,
})

_QTY_TOLERANCE_DEFAULT = Decimal("1")
_DOLLAR_TOLERANCE = Decimal("1.00")


@dataclass(frozen=True)
class ReconcileResult:
    """Summary of a single reconcile_once() pass."""

    orders_checked: int
    orders_updated: int          # broker reported a different status than OMS
    position_mismatches: int     # symbols where qty deviation exceeded tolerance
    kill_switch_tripped: bool


class Reconciler:
    """Periodic reconciliation between OMS state and broker state.

    The OMS exposes a `_on_broker_event` callback (via `register_event_callback`
    at construction time). Reconciler drives the same callback to backfill any
    state changes the OMS missed (e.g. fills that arrived while the stream was
    disconnected, or cancels issued by the broker).
    """

    def __init__(
        self,
        oms: OMS,
        broker: Broker,
        kill: KillSwitchEngine,
        interval_secs: int = 60,
        qty_tolerance: Decimal = _QTY_TOLERANCE_DEFAULT,
    ) -> None:
        self._oms = oms
        self._broker = broker
        self._kill = kill
        self._interval_secs = interval_secs
        self._qty_tolerance = qty_tolerance
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ── Public API ────────────────────────────────────────────────────────────

    def reconcile_once(self, ts: datetime) -> ReconcileResult:
        """Run one reconcile pass synchronously. Thread-safe; can be called from tests."""
        open_orders = self._oms.list_open_orders()
        orders_updated = self._reconcile_orders(open_orders)
        mismatches, tripped = self._reconcile_positions()
        return ReconcileResult(
            orders_checked=len(open_orders),
            orders_updated=orders_updated,
            position_mismatches=mismatches,
            kill_switch_tripped=tripped,
        )

    def start(self) -> None:
        """Start the background reconciliation loop."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="reconciler",
        )
        self._thread.start()
        logger.info("Reconciler: started (interval=%ds)", self._interval_secs)

    def stop(self) -> None:
        """Signal the loop to stop and wait for the thread to exit (max 5s)."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # ── Background loop ───────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            ts = datetime.now(UTC)
            try:
                result = self.reconcile_once(ts)
                if result.orders_updated or result.position_mismatches:
                    logger.info(
                        "Reconciler: %d orders updated, %d position mismatches",
                        result.orders_updated,
                        result.position_mismatches,
                    )
            except Exception:
                logger.exception("Reconciler: error during reconcile_once")
            self._stop_event.wait(self._interval_secs)

    # ── Order reconciliation ──────────────────────────────────────────────────

    def _reconcile_orders(self, open_orders: list[Order]) -> int:
        """Check each open OMS order against broker; call OMS callback on drift."""
        updated = 0
        for order in open_orders:
            if order.broker_order_id is None:
                continue
            try:
                status = self._broker.get_order(order.broker_order_id)
            except Exception:
                logger.warning(
                    "Reconciler: could not fetch broker status for %s",
                    order.broker_order_id,
                    exc_info=True,
                )
                continue

            if self._status_requires_update(order.state, status.state):
                event = BrokerOrderEvent(
                    broker_order_id=status.broker_order_id,
                    client_order_id=status.client_order_id,
                    new_state=status.state,
                    fill=None,   # fills come via stream; here we just sync state
                    timestamp=status.updated_at,
                )
                self._oms.on_broker_event(event)
                updated += 1
        return updated

    # ── Position reconciliation ───────────────────────────────────────────────

    def _reconcile_positions(self) -> tuple[int, bool]:
        """Compare expected positions (from OMS fills) vs broker positions.

        Returns (num_mismatches, kill_switch_tripped).
        """
        expected = self._compute_expected_positions()
        try:
            broker_positions: dict[str, BrokerPosition] = {
                p.symbol: p for p in self._broker.list_positions()
            }
        except Exception:
            logger.warning("Reconciler: could not fetch broker positions", exc_info=True)
            return 0, False

        all_symbols = set(expected.keys()) | set(broker_positions.keys())
        mismatches: list[str] = []

        for sym in all_symbols:
            our_qty = expected.get(sym, Decimal("0"))
            broker_pos = broker_positions.get(sym)
            broker_qty = broker_pos.qty if broker_pos is not None else Decimal("0")
            qty_drift = abs(our_qty - broker_qty)
            price = broker_pos.current_price if broker_pos is not None else Decimal("0")
            dollar_drift = qty_drift * price
            if qty_drift >= self._qty_tolerance or dollar_drift > _DOLLAR_TOLERANCE:
                logger.error(
                    "Reconciler: position mismatch on %s — expected=%.4f broker=%.4f "
                    "(qty_drift=%.4f dollar_drift=$%.2f)",
                    sym, our_qty, broker_qty, qty_drift, dollar_drift,
                )
                mismatches.append(sym)

        tripped = False
        if mismatches:
            self._kill.trip_reconciliation_break()
            tripped = True

        return len(mismatches), tripped

    def _compute_expected_positions(self) -> dict[str, Decimal]:
        """Sum all OMS fills into a net position per symbol."""
        positions: dict[str, Decimal] = {}
        for order in self._oms.list_orders():
            for fill in self._oms.get_fills(order.id):
                sign = Decimal("1") if fill.side == OrderSide.BUY else Decimal("-1")
                positions[fill.symbol] = (
                    positions.get(fill.symbol, Decimal("0")) + sign * fill.qty
                )
        # Remove zeroed-out positions
        return {sym: qty for sym, qty in positions.items() if qty != Decimal("0")}

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _status_requires_update(
        oms_state: OrderState, broker_state: BrokerOrderState
    ) -> bool:
        """True if broker reports a terminal state that OMS hasn't recorded yet."""
        if oms_state in (
            OrderState.FILLED, OrderState.CANCELLED,
            OrderState.REJECTED, OrderState.EXPIRED,
        ):
            return False
        return broker_state in (
            BrokerOrderState.FILLED,
            BrokerOrderState.PARTIALLY_FILLED,
            BrokerOrderState.CANCELED,
            BrokerOrderState.REJECTED,
            BrokerOrderState.EXPIRED,
        )
