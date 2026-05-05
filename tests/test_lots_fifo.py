"""Tests for execution/lots.py — FIFO and LIFO lot consumption."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from core.types import AgentId, Fill, LotMethod, OrderSide, new_id
from execution.lots import LotLedger

# ── Helpers ───────────────────────────────────────────────────────────────────


def _ts(day: int) -> datetime:
    return datetime(2026, 1, day, 10, 0, tzinfo=UTC)


def _fill(
    side: OrderSide,
    qty: str,
    price: str,
    day: int = 1,
    agent: AgentId = AgentId.HAIKU,
    symbol: str = "SPY",
) -> Fill:
    return Fill(
        id=new_id(),
        order_id=new_id(),
        agent_id=agent,
        symbol=symbol,
        side=side,
        qty=Decimal(qty),
        price=Decimal(price),
        timestamp=_ts(day),
    )


# ── open_lot ─────────────────────────────────────────────────────────────────


def test_open_lot_creates_lot_with_correct_fields() -> None:
    ledger = LotLedger()
    fill = _fill(OrderSide.BUY, "10", "100.00", day=1)
    lot = ledger.open_lot(fill)

    assert lot.symbol == "SPY"
    assert lot.qty == Decimal("10")
    assert lot.entry_price == Decimal("100.00")
    assert lot.entry_date == date(2026, 1, 1)
    assert lot.remaining_qty == Decimal("10")
    assert lot.is_closed is False
    assert lot.entry_fill_id == fill.id


def test_open_lot_requires_buy_fill() -> None:
    ledger = LotLedger()
    fill = _fill(OrderSide.SELL, "10", "100.00")
    with pytest.raises(ValueError, match="BUY"):
        ledger.open_lot(fill)


def test_open_lot_registers_in_open_lots() -> None:
    ledger = LotLedger()
    fill = _fill(OrderSide.BUY, "5", "50.00")
    lot = ledger.open_lot(fill)
    assert lot in ledger.open_lots(AgentId.HAIKU, "SPY")


# ── close_lots FIFO ───────────────────────────────────────────────────────────


def test_fifo_closes_oldest_lot_first() -> None:
    ledger = LotLedger()
    lot1 = ledger.open_lot(_fill(OrderSide.BUY, "10", "100", day=1))
    lot2 = ledger.open_lot(_fill(OrderSide.BUY, "10", "110", day=3))

    sell = _fill(OrderSide.SELL, "10", "120", day=5)
    closed = ledger.close_lots(AgentId.HAIKU, "SPY", Decimal("10"), sell, LotMethod.FIFO)

    assert len(closed) == 1
    assert closed[0].id == lot1.id
    assert closed[0].is_closed is True
    # lot2 should still be open
    assert ledger.open_lots(AgentId.HAIKU, "SPY")[0].id == lot2.id


def test_fifo_partial_close_updates_remaining() -> None:
    ledger = LotLedger()
    ledger.open_lot(_fill(OrderSide.BUY, "10", "100", day=1))

    sell = _fill(OrderSide.SELL, "4", "120", day=5)
    closed = ledger.close_lots(AgentId.HAIKU, "SPY", Decimal("4"), sell, LotMethod.FIFO)

    assert len(closed) == 1
    affected = closed[0]
    assert affected.remaining_qty == Decimal("6")
    assert affected.is_closed is False
    assert affected.exit_date is None   # not fully closed — no exit stamp
    assert affected.exit_price is None


def test_fifo_full_close_sets_exit_fields() -> None:
    ledger = LotLedger()
    ledger.open_lot(_fill(OrderSide.BUY, "10", "100", day=1))

    sell = _fill(OrderSide.SELL, "10", "130", day=5)
    closed = ledger.close_lots(AgentId.HAIKU, "SPY", Decimal("10"), sell, LotMethod.FIFO)

    assert closed[0].is_closed is True
    assert closed[0].remaining_qty == Decimal("0")
    assert closed[0].exit_date == date(2026, 1, 5)
    assert closed[0].exit_price == Decimal("130")
    assert closed[0].exit_fill_id == sell.id


def test_fifo_spans_multiple_lots() -> None:
    ledger = LotLedger()
    ledger.open_lot(_fill(OrderSide.BUY, "5", "100", day=1))
    ledger.open_lot(_fill(OrderSide.BUY, "5", "110", day=2))
    ledger.open_lot(_fill(OrderSide.BUY, "5", "120", day=3))

    sell = _fill(OrderSide.SELL, "12", "130", day=5)
    closed = ledger.close_lots(AgentId.HAIKU, "SPY", Decimal("12"), sell, LotMethod.FIFO)

    assert len(closed) == 3
    # First two fully closed, last partially consumed
    assert closed[0].is_closed is True
    assert closed[1].is_closed is True
    assert closed[2].is_closed is False
    assert closed[2].remaining_qty == Decimal("3")


# ── close_lots LIFO ───────────────────────────────────────────────────────────


def test_lifo_closes_newest_lot_first() -> None:
    ledger = LotLedger()
    lot1 = ledger.open_lot(_fill(OrderSide.BUY, "10", "100", day=1))
    lot2 = ledger.open_lot(_fill(OrderSide.BUY, "10", "110", day=3))

    sell = _fill(OrderSide.SELL, "10", "120", day=5)
    closed = ledger.close_lots(AgentId.HAIKU, "SPY", Decimal("10"), sell, LotMethod.LIFO)

    assert len(closed) == 1
    assert closed[0].id == lot2.id   # newest first
    assert closed[0].is_closed is True
    open_remaining = ledger.open_lots(AgentId.HAIKU, "SPY")
    assert open_remaining[0].id == lot1.id


# ── error handling ────────────────────────────────────────────────────────────


def test_close_lots_insufficient_qty_raises() -> None:
    ledger = LotLedger()
    ledger.open_lot(_fill(OrderSide.BUY, "5", "100", day=1))

    sell = _fill(OrderSide.SELL, "10", "120", day=5)
    with pytest.raises(ValueError, match="Cannot close"):
        ledger.close_lots(AgentId.HAIKU, "SPY", Decimal("10"), sell)


def test_close_lots_requires_sell_fill() -> None:
    ledger = LotLedger()
    ledger.open_lot(_fill(OrderSide.BUY, "5", "100"))
    buy = _fill(OrderSide.BUY, "5", "120", day=2)
    with pytest.raises(ValueError, match="SELL"):
        ledger.close_lots(AgentId.HAIKU, "SPY", Decimal("5"), buy)


# ── cross-agent isolation ─────────────────────────────────────────────────────


def test_open_lots_scoped_to_agent() -> None:
    ledger = LotLedger()
    ledger.open_lot(_fill(OrderSide.BUY, "10", "100", agent=AgentId.HAIKU))
    ledger.open_lot(_fill(OrderSide.BUY, "10", "100", agent=AgentId.SONNET))

    haiku_lots = ledger.open_lots(AgentId.HAIKU, "SPY")
    sonnet_lots = ledger.open_lots(AgentId.SONNET, "SPY")
    assert len(haiku_lots) == 1
    assert len(sonnet_lots) == 1
    assert haiku_lots[0].agent_id == AgentId.HAIKU
    assert sonnet_lots[0].agent_id == AgentId.SONNET


# ── total_open_qty ────────────────────────────────────────────────────────────


def test_total_open_qty_after_partial_close() -> None:
    ledger = LotLedger()
    ledger.open_lot(_fill(OrderSide.BUY, "10", "100", day=1))
    ledger.open_lot(_fill(OrderSide.BUY, "5", "105", day=2))

    sell = _fill(OrderSide.SELL, "6", "120", day=5)
    ledger.close_lots(AgentId.HAIKU, "SPY", Decimal("6"), sell, LotMethod.FIFO)

    assert ledger.total_open_qty(AgentId.HAIKU, "SPY") == Decimal("9")


# ── Persistence + idempotency ────────────────────────────────────────────────


def test_persistence_round_trip(tmp_path):
    db = str(tmp_path / "lots.db")
    ledger = LotLedger(db_path=db)
    buy = _fill(OrderSide.BUY, "10", "100", day=1)
    ledger.book_fill(buy)

    # Reload from disk
    reloaded = LotLedger(db_path=db)
    lots = reloaded.all_lots()
    assert len(lots) == 1
    assert lots[0].entry_fill_id == buy.id
    assert lots[0].remaining_qty == Decimal("10")
    assert reloaded.total_open_qty(AgentId.HAIKU, "SPY") == Decimal("10")


def test_book_fill_is_idempotent(tmp_path):
    db = str(tmp_path / "lots.db")
    ledger = LotLedger(db_path=db)
    buy = _fill(OrderSide.BUY, "5", "100", day=1)
    ledger.book_fill(buy)
    ledger.book_fill(buy)  # duplicate event — must no-op
    ledger.book_fill(buy)
    assert len(ledger.all_lots()) == 1


# ── Symbol normalization ──────────────────────────────────────────────────────


def test_open_lot_normalizes_crypto_symbol() -> None:
    """Slashed crypto form ("BTC/USD") must be persisted as "BTCUSD" so it
    matches the canonical form used by the broker positions API and
    AgentStateTracker. Without this, a single position can produce two lots
    when fills arrive via different paths."""
    ledger = LotLedger()
    ledger.open_lot(_fill(OrderSide.BUY, "0.01", "80000", symbol="BTC/USD"))
    lots = ledger.all_lots()
    assert len(lots) == 1
    assert lots[0].symbol == "BTCUSD"


def test_open_lot_idempotent_across_symbol_forms() -> None:
    """Two fills on the same logical position arriving in different symbol
    forms must collapse to one canonical-symbol view, not two distinct lots
    that downstream lookups can miss."""
    ledger = LotLedger()
    ledger.open_lot(_fill(OrderSide.BUY, "0.01", "80000", symbol="BTC/USD"))
    ledger.open_lot(_fill(OrderSide.BUY, "0.01", "80100", symbol="BTCUSD"))
    # Two lots (distinct fill ids) but both under canonical symbol.
    assert ledger.total_open_qty(AgentId.HAIKU, "BTCUSD") == Decimal("0.02")
    assert ledger.total_open_qty(AgentId.HAIKU, "BTC/USD") == Decimal("0.02")  # also accepts slashed input


def test_close_lots_accepts_either_symbol_form() -> None:
    """SELL paths may pass either form; close_lots normalizes the lookup."""
    ledger = LotLedger()
    ledger.open_lot(_fill(OrderSide.BUY, "0.02", "80000", symbol="BTCUSD"))
    sell = _fill(OrderSide.SELL, "0.01", "82000", day=2, symbol="BTC/USD")
    affected = ledger.close_lots(
        agent_id=AgentId.HAIKU, symbol="BTC/USD", qty=Decimal("0.01"), exit_fill=sell,
    )
    assert len(affected) == 1
    assert affected[0].remaining_qty == Decimal("0.01")
