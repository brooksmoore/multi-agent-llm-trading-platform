"""Tax lot ledger with FIFO and LIFO consumption.

Open one lot per BUY fill; consume lots (FIFO or LIFO) for each SELL fill.
Thread-safe; all mutations hold a Lock.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from decimal import Decimal

from core.types import AgentId, Fill, Lot, LotId, LotMethod, OrderSide, new_id


class LotLedger:
    """In-memory ledger of all tax lots across all agents."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lots: dict[LotId, Lot] = {}

    # ── Write operations ──────────────────────────────────────────────────────

    def open_lot(self, fill: Fill) -> Lot:
        """Create and register a new lot from a BUY fill."""
        if fill.side != OrderSide.BUY:
            raise ValueError(f"open_lot requires a BUY fill, got {fill.side}")
        lot = Lot(
            id=new_id(),
            agent_id=fill.agent_id,
            symbol=fill.symbol,
            qty=fill.qty,
            entry_price=fill.price,
            entry_date=fill.timestamp.date(),
            entry_fill_id=fill.id,
        )
        with self._lock:
            self._lots[lot.id] = lot
        return lot

    def close_lots(
        self,
        agent_id: AgentId,
        symbol: str,
        qty: Decimal,
        exit_fill: Fill,
        method: LotMethod = LotMethod.FIFO,
    ) -> list[Lot]:
        """Consume `qty` shares from open lots; return the (updated) affected lots.

        FIFO: oldest entry date first.
        LIFO: newest entry date first.
        Raises ValueError if insufficient open qty.
        """
        if exit_fill.side != OrderSide.SELL:
            raise ValueError(f"close_lots requires a SELL fill, got {exit_fill.side}")

        with self._lock:
            candidates = [
                lot for lot in self._lots.values()
                if lot.agent_id == agent_id
                and lot.symbol == symbol
                and not lot.is_closed
                and lot.remaining_qty > Decimal("0")
            ]
            candidates.sort(
                key=lambda lot: lot.entry_date,
                reverse=(method == LotMethod.LIFO),
            )

            available = sum(lot.remaining_qty for lot in candidates)
            if qty > available:
                raise ValueError(
                    f"Cannot close {qty} of {symbol} for {agent_id}: "
                    f"only {available} available across {len(candidates)} lots"
                )

            exit_date = exit_fill.timestamp.date()
            remaining_to_close = qty
            affected: list[Lot] = []

            for lot in candidates:
                if remaining_to_close <= Decimal("0"):
                    break
                consume = min(lot.remaining_qty, remaining_to_close)
                new_remaining = lot.remaining_qty - consume
                is_closed = new_remaining == Decimal("0")
                updated = replace(
                    lot,
                    remaining_qty=new_remaining,
                    is_closed=is_closed,
                    exit_fill_id=exit_fill.id if is_closed else lot.exit_fill_id,
                    exit_date=exit_date if is_closed else lot.exit_date,
                    exit_price=exit_fill.price if is_closed else lot.exit_price,
                )
                self._lots[lot.id] = updated
                affected.append(updated)
                remaining_to_close -= consume

        return affected

    # ── Read operations ───────────────────────────────────────────────────────

    def open_lots(self, agent_id: AgentId, symbol: str) -> list[Lot]:
        """Return all open (not fully consumed) lots for agent+symbol."""
        with self._lock:
            return [
                lot for lot in self._lots.values()
                if lot.agent_id == agent_id
                and lot.symbol == symbol
                and not lot.is_closed
            ]

    def all_lots(self) -> list[Lot]:
        with self._lock:
            return list(self._lots.values())

    def total_open_qty(self, agent_id: AgentId, symbol: str) -> Decimal:
        return sum(
            (lot.remaining_qty for lot in self.open_lots(agent_id, symbol)), Decimal("0")
        )
