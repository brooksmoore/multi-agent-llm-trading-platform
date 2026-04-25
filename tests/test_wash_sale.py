"""Tests for execution/tax.py — wash-sale rule and harvesting candidates."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from core.types import AgentId, Lot, new_id
from execution.tax import WASH_SALE_WINDOW_DAYS, WashSaleChecker

# ── Helpers ───────────────────────────────────────────────────────────────────


def _lot(
    *,
    symbol: str = "SPY",
    agent: AgentId = AgentId.HAIKU,
    entry_price: str,
    exit_price: str | None = None,
    entry_day: int = 1,
    exit_day: int | None = None,
) -> Lot:
    lot = Lot(
        id=new_id(),
        agent_id=agent,
        symbol=symbol,
        qty=Decimal("10"),
        entry_price=Decimal(entry_price),
        entry_date=date(2026, 1, entry_day),
        entry_fill_id=new_id(),
    )
    if exit_price is not None and exit_day is not None:
        from dataclasses import replace
        lot = replace(
            lot,
            exit_price=Decimal(exit_price),
            exit_date=date(2026, 1, exit_day),
            exit_fill_id=new_id(),
            remaining_qty=Decimal("0"),
            is_closed=True,
        )
    return lot


# ── record_sale ───────────────────────────────────────────────────────────────


def test_record_sale_loss_adds_record() -> None:
    checker = WashSaleChecker()
    lot = _lot(entry_price="100", exit_price="80", entry_day=1, exit_day=5)
    checker.record_sale(lot)
    assert len(checker.all_records()) == 1


def test_record_sale_gain_is_ignored() -> None:
    checker = WashSaleChecker()
    lot = _lot(entry_price="100", exit_price="120", entry_day=1, exit_day=5)
    checker.record_sale(lot)
    assert len(checker.all_records()) == 0


def test_record_sale_break_even_is_ignored() -> None:
    checker = WashSaleChecker()
    lot = _lot(entry_price="100", exit_price="100", entry_day=1, exit_day=5)
    checker.record_sale(lot)
    assert len(checker.all_records()) == 0


def test_record_sale_unclosed_lot_is_ignored() -> None:
    checker = WashSaleChecker()
    lot = _lot(entry_price="100")  # no exit
    checker.record_sale(lot)
    assert len(checker.all_records()) == 0


# ── is_blocked ────────────────────────────────────────────────────────────────


def test_not_blocked_without_any_loss_sale() -> None:
    checker = WashSaleChecker()
    assert checker.is_blocked(AgentId.HAIKU, "SPY", date(2026, 1, 10)) is False


def test_blocked_on_same_day_as_loss_sale() -> None:
    checker = WashSaleChecker()
    lot = _lot(entry_price="100", exit_price="80", entry_day=1, exit_day=10)
    checker.record_sale(lot)
    # Buy on same day as sale → blocked (day 0)
    assert checker.is_blocked(AgentId.HAIKU, "SPY", date(2026, 1, 10)) is True


def test_blocked_within_30_days_of_loss_sale() -> None:
    checker = WashSaleChecker()
    lot = _lot(entry_price="100", exit_price="80", entry_day=1, exit_day=1)
    checker.record_sale(lot)  # sold on Jan 1
    # Buy on Jan 31 = 30 days later → still blocked (window includes day 30)
    assert checker.is_blocked(AgentId.HAIKU, "SPY", date(2026, 1, 31)) is True


def test_not_blocked_31_days_after_loss_sale() -> None:
    checker = WashSaleChecker()
    lot = _lot(entry_price="100", exit_price="80", entry_day=1, exit_day=1)
    checker.record_sale(lot)  # sold on Jan 1
    # Buy on Feb 1 = 31 days later → just outside window
    assert checker.is_blocked(AgentId.HAIKU, "SPY", date(2026, 2, 1)) is False


def test_not_blocked_different_symbol() -> None:
    checker = WashSaleChecker()
    lot = _lot(symbol="SPY", entry_price="100", exit_price="80", entry_day=1, exit_day=1)
    checker.record_sale(lot)
    assert checker.is_blocked(AgentId.HAIKU, "QQQ", date(2026, 1, 5)) is False


def test_not_blocked_different_agent() -> None:
    checker = WashSaleChecker()
    lot = _lot(
        agent=AgentId.HAIKU,
        entry_price="100",
        exit_price="80",
        entry_day=1,
        exit_day=1,
    )
    checker.record_sale(lot)
    # SONNET buying SPY is not blocked by HAIKU's loss sale
    assert checker.is_blocked(AgentId.SONNET, "SPY", date(2026, 1, 5)) is False


def test_blocked_at_window_boundary() -> None:
    from datetime import timedelta
    checker = WashSaleChecker()
    sale_date = date(2026, 3, 1)  # use March so +31 days stays in-range
    lot = Lot(
        id=new_id(),
        agent_id=AgentId.HAIKU,
        symbol="SPY",
        qty=Decimal("10"),
        entry_price=Decimal("100"),
        entry_date=date(2026, 2, 1),
        entry_fill_id=new_id(),
        exit_price=Decimal("80"),
        exit_date=sale_date,
        exit_fill_id=new_id(),
        remaining_qty=Decimal("0"),
        is_closed=True,
    )
    checker.record_sale(lot)
    # Exactly WASH_SALE_WINDOW_DAYS days later → still blocked
    boundary = sale_date + timedelta(days=WASH_SALE_WINDOW_DAYS)
    assert checker.is_blocked(AgentId.HAIKU, "SPY", boundary) is True
    # One day past the window → not blocked
    past_window = sale_date + timedelta(days=WASH_SALE_WINDOW_DAYS + 1)
    assert checker.is_blocked(AgentId.HAIKU, "SPY", past_window) is False


# ── harvesting_candidates ─────────────────────────────────────────────────────


def test_harvesting_candidates_returns_loss_lots() -> None:
    checker = WashSaleChecker()
    lots = [
        _lot(entry_price="100"),   # unrealized loss if price < 100
        _lot(entry_price="50"),    # unrealized gain if price > 50
    ]
    prices = {"SPY": Decimal("80")}
    candidates = checker.harvesting_candidates(lots, prices)
    assert len(candidates) == 1
    assert candidates[0].entry_price == Decimal("100")


def test_harvesting_candidates_excludes_closed_lots() -> None:
    checker = WashSaleChecker()
    lots = [_lot(entry_price="100", exit_price="80", entry_day=1, exit_day=5)]
    prices = {"SPY": Decimal("80")}
    candidates = checker.harvesting_candidates(lots, prices)
    assert candidates == []


def test_harvesting_candidates_ignores_missing_prices() -> None:
    checker = WashSaleChecker()
    lots = [_lot(symbol="AAPL", entry_price="200")]
    prices: dict[str, Decimal] = {}
    candidates = checker.harvesting_candidates(lots, prices)
    assert candidates == []
