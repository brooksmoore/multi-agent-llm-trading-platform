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

from core.events import EventBus, KillSwitchResetEvent, KillSwitchTrippedEvent
from core.types import AgentId, Fill, KillSwitchState, Order, OrderSide, OrderState, new_id
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
        bus: EventBus | None = None,
        orphan_agent_id: AgentId = AgentId.HAIKU,
    ) -> None:
        self._oms = oms
        self._broker = broker
        self._kill = kill
        self._interval_secs = interval_secs
        self._qty_tolerance = qty_tolerance
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._bus = bus
        self._last_kill_state: KillSwitchState = kill.state
        self._orphan_agent_id = orphan_agent_id

    # ── Public API ────────────────────────────────────────────────────────────

    def reconcile_once(self, ts: datetime) -> ReconcileResult:
        """Run one reconcile pass synchronously. Thread-safe; can be called from tests."""
        # Heartbeat watchdog: KillSwitchEngine.check_heartbeat() flips the
        # state to HEARTBEAT_MISSED if the writer thread is overdue. Nobody
        # else calls it on a schedule, so we piggyback on the reconciler tick
        # (every 60s) to keep heartbeat detection alive. The state-transition
        # block below converts that into a KillSwitchTrippedEvent + a
        # dedicated HeartbeatMissedEvent for subscribers that want one.
        previously_ok = self._kill.state == KillSwitchState.OK
        self._kill.check_heartbeat(ts)
        if (
            self._bus is not None
            and previously_ok
            and self._kill.state == KillSwitchState.HEARTBEAT_MISSED
        ):
            try:
                from core.events import HeartbeatMissedEvent
                self._bus.publish(HeartbeatMissedEvent())
            except Exception:
                logger.warning(
                    "Reconciler: failed to publish HeartbeatMissed", exc_info=True,
                )
        open_orders = self._oms.list_open_orders()
        orders_updated = self._reconcile_orders(open_orders)
        mismatches, tripped = self._reconcile_positions(ts)
        # Auto-clear a prior reconciliation break once books agree again.
        if mismatches == 0:
            self._kill.clear_reconciliation_break()
        # Publish state-change events when kill switch flips. The
        # KillSwitchEngine doesn't carry an event bus, so we observe its state
        # transitions here and forward them to subscribers (alerts/Telegram).
        new_state = self._kill.state
        if self._bus is not None and new_state != self._last_kill_state:
            if new_state == KillSwitchState.OK:
                self._bus.publish(KillSwitchResetEvent())
            else:
                reason = "reconciliation_break" if tripped else str(new_state)
                self._bus.publish(KillSwitchTrippedEvent(
                    new_state=new_state, reason=reason,
                ))
            self._last_kill_state = new_state
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
                # Synthesize a Fill for any qty the broker has filled that
                # isn't yet recorded locally. This is the polling fallback
                # when the trade-update stream is down or events were missed.
                fill: Fill | None = None
                delta_qty = status.filled_qty - order.filled_qty
                broker_terminal_filled = status.state in (
                    BrokerOrderState.PARTIALLY_FILLED, BrokerOrderState.FILLED,
                )
                if broker_terminal_filled and delta_qty > Decimal("0"):
                    fill = Fill(
                        id=new_id(),
                        order_id=order.id,
                        agent_id=order.agent_id,
                        symbol=order.symbol,
                        side=order.side,
                        qty=delta_qty,
                        price=status.avg_fill_price or Decimal("0"),
                        timestamp=status.updated_at,
                        commission=Decimal("0"),
                        is_partial=status.state != BrokerOrderState.FILLED,
                    )
                elif (
                    broker_terminal_filled
                    and status.state == BrokerOrderState.FILLED
                    and delta_qty <= Decimal("0")
                ):
                    # Broker confirms FILLED and we already have all the fill
                    # qty recorded locally — order is stuck non-terminal due to
                    # qty-precision drift. Force-close locally; don't emit a
                    # fill=None FILLED event (OMS would reject it).
                    self._oms.force_close_filled(order.id, ts=status.updated_at)
                    updated += 1
                    continue
                event = BrokerOrderEvent(
                    broker_order_id=status.broker_order_id,
                    client_order_id=status.client_order_id,
                    new_state=status.state,
                    fill=fill,
                    timestamp=status.updated_at,
                )
                self._oms.on_broker_event(event)
                updated += 1
        return updated

    # ── Position reconciliation ───────────────────────────────────────────────

    def _reconcile_positions(self, ts: datetime) -> tuple[int, bool]:
        """Compare expected positions (from OMS fills) vs broker positions.

        When the broker holds a position the OMS has no record of (our_qty == 0,
        broker_qty > 0), the fill was lost — typically a websocket outage during
        a fill event. In that case we write the position back into the OMS as a
        ghost fill so the books agree on the next pass instead of tripping the
        kill switch forever.

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

            if qty_drift < self._qty_tolerance and dollar_drift <= _DOLLAR_TOLERANCE:
                continue

            # Broker has a position we have zero record of — adopt it.
            if our_qty == Decimal("0") and broker_qty > Decimal("0"):
                logger.warning(
                    "Reconciler: orphan position %s broker=%.6f @ $%.4f — "
                    "adopting into OMS as agent=%s",
                    sym, broker_qty, price, self._orphan_agent_id,
                )
                try:
                    self._oms.adopt_orphan_position(
                        symbol=sym,
                        qty=broker_qty,
                        price=price,
                        agent_id=self._orphan_agent_id,
                        ts=ts,
                    )
                except Exception:
                    logger.exception(
                        "Reconciler: adopt_orphan_position failed for %s", sym)
                    mismatches.append(sym)
                # Don't count this tick as a mismatch — books are now in sync.
                continue

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
            if self._bus is not None:
                try:
                    from core.events import ReconciliationBreakEvent
                    # Publish per-symbol so each break is independently
                    # actionable. Drop a single rolled-up event if the list
                    # is large (defensive against position blowouts).
                    for sym in mismatches[:5]:
                        self._bus.publish(ReconciliationBreakEvent(symbol=sym))
                except Exception:
                    logger.warning(
                        "Reconciler: failed to publish ReconciliationBreak", exc_info=True,
                    )

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
