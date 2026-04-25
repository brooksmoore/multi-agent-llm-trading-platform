"""Wash-sale rule enforcement and tax-loss harvesting candidate finder.

IRS wash-sale rule: a loss is disallowed if you buy substantially identical
securities within 30 calendar days before or after the sale. This module
enforces the *after* direction only (loss sale → 30-day buy block), which is
the relevant direction for a bot that never pre-purchases.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from core.types import AgentId, Lot

WASH_SALE_WINDOW_DAYS = 30


@dataclass(frozen=True)
class WashSaleRecord:
    symbol: str
    agent_id: AgentId
    sale_date: date
    loss: Decimal  # negative number


class WashSaleChecker:
    """Thread-safe wash-sale tracker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: list[WashSaleRecord] = []

    def record_sale(self, lot: Lot) -> None:
        """Call when a lot is closed. Records only loss sales (pnl < 0)."""
        pnl = lot.realized_pnl
        if pnl is None or pnl >= Decimal("0"):
            return
        if lot.exit_date is None:
            return
        with self._lock:
            self._records.append(
                WashSaleRecord(
                    symbol=lot.symbol,
                    agent_id=lot.agent_id,
                    sale_date=lot.exit_date,
                    loss=pnl,
                )
            )

    def is_blocked(self, agent_id: AgentId, symbol: str, buy_date: date) -> bool:
        """Return True if buying `symbol` on `buy_date` would trigger a wash sale."""
        with self._lock:
            for rec in self._records:
                if rec.agent_id != agent_id or rec.symbol != symbol:
                    continue
                days_since = (buy_date - rec.sale_date).days
                if 0 <= days_since <= WASH_SALE_WINDOW_DAYS:
                    return True
        return False

    def harvesting_candidates(
        self,
        lots: list[Lot],
        prices: dict[str, Decimal],
    ) -> list[Lot]:
        """Return open lots whose current price is below entry price (unrealized loss).

        These are candidates for tax-loss harvesting. Caller is responsible for
        checking wash-sale rules before executing the harvest.
        """
        candidates: list[Lot] = []
        for lot in lots:
            if lot.is_closed:
                continue
            price = prices.get(lot.symbol)
            if price is None:
                continue
            if price < lot.entry_price:
                candidates.append(lot)
        return candidates

    def all_records(self) -> list[WashSaleRecord]:
        with self._lock:
            return list(self._records)
