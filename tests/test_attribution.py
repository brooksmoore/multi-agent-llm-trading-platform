"""Per-sleeve P&L attribution tests (T1.5).

Seeds a LotLedger + OMSStore with deterministic BUY+SELL fills across
multiple agents, then asserts compute_daily_pnl returns the expected
realized sum per agent (via FIFO matching of fills) and marks open
lots to the latest bar close.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from core.types import AgentId, Fill, OrderSide, new_id
from data.market import Bar
from execution.lots import LotLedger
from execution.oms import _serialize_fill
from execution.oms_store import EventKind, OMSStore
from ops.agent_pnl_store import AgentPnLStore
from ops.attribution import PnLBreakdown, compute_daily_pnl


def _fill(agent: AgentId, symbol: str, side: OrderSide, qty: str, price: str) -> Fill:
    return Fill(
        id=new_id(), order_id=new_id(), agent_id=agent,
        symbol=symbol, side=side,
        qty=Decimal(qty), price=Decimal(price),
        timestamp=datetime(2026, 5, 11, 16, 0, tzinfo=UTC),
    )


def _seed(lots: LotLedger, store: OMSStore, fill: Fill) -> None:
    """Persist a fill into both the lot ledger and the OMS event log."""
    lots.book_fill(fill)
    store.append(EventKind.FILL_RECEIVED, fill.order_id, _serialize_fill(fill), fill.timestamp)


@dataclass
class _StubMarket:
    """Minimal MarketData stand-in returning a fixed close per symbol."""

    closes: dict[str, Decimal]

    def get_latest_bar(self, symbol: str) -> Bar | None:
        if symbol not in self.closes:
            return None
        c = self.closes[symbol]
        return Bar(
            symbol=symbol,
            timestamp=datetime(2026, 5, 11, 16, 0, tzinfo=UTC),
            open=c, high=c, low=c, close=c, volume=1_000_000,
        )

    # Unused by attribution but required by the MarketData Protocol shape.
    def get_bars(self, *args: object, **kwargs: object) -> list[Bar]:  # noqa: ARG002
        return []

    def get_bars_batch(self, *args: object, **kwargs: object) -> dict[str, list[Bar]]:  # noqa: ARG002
        return {}

    def get_latest_quote(self, *args: object, **kwargs: object) -> None:  # noqa: ARG002
        return None


def test_attribution_realized_sums_per_agent(tmp_path: Path) -> None:
    """Each agent's realized P&L = FIFO match of their own SELLs against BUYs."""
    lots = LotLedger()
    store = OMSStore(tmp_path / "oms.db")

    # Haiku: buys 10 SPY @ 400, sells all @ 410 → +100 realized
    _seed(lots, store, _fill(AgentId.HAIKU, "SPY", OrderSide.BUY, "10", "400"))
    _seed(lots, store, _fill(AgentId.HAIKU, "SPY", OrderSide.SELL, "10", "410"))

    # Sonnet: buys 5 NVDA @ 100, sells 3 @ 120 → +60 realized; 2 still open
    _seed(lots, store, _fill(AgentId.SONNET, "NVDA", OrderSide.BUY, "5", "100"))
    _seed(lots, store, _fill(AgentId.SONNET, "NVDA", OrderSide.SELL, "3", "120"))

    # Opus: buys 2 TSM @ 200 (no exit) → 0 realized; 2 open
    _seed(lots, store, _fill(AgentId.OPUS, "TSM", OrderSide.BUY, "2", "200"))

    market = _StubMarket(closes={"NVDA": Decimal("130"), "TSM": Decimal("210")})

    pnl = compute_daily_pnl(lots, store, market)

    assert pnl[AgentId.HAIKU].realized == Decimal("100")
    assert pnl[AgentId.SONNET].realized == Decimal("60")
    assert pnl[AgentId.OPUS].realized == Decimal("0")
    store.close()


def test_attribution_unrealized_uses_latest_bar(tmp_path: Path) -> None:
    """Open lots mark to MarketData.get_latest_bar(symbol).close."""
    lots = LotLedger()
    store = OMSStore(tmp_path / "oms.db")
    _seed(lots, store, _fill(AgentId.OPUS, "TSM", OrderSide.BUY, "10", "200"))
    market = _StubMarket(closes={"TSM": Decimal("215")})

    pnl = compute_daily_pnl(lots, store, market)

    # 10 shares * (215 - 200) = 150
    assert pnl[AgentId.OPUS].unrealized == Decimal("150")
    assert pnl[AgentId.OPUS].realized == Decimal("0")
    assert pnl[AgentId.OPUS].total == Decimal("150")
    assert pnl[AgentId.OPUS].num_open_lots == 1
    assert pnl[AgentId.OPUS].num_closed_lots == 0
    store.close()


def test_attribution_partial_exit_splits_realized_and_unrealized(tmp_path: Path) -> None:
    """A partial trim must contribute to BOTH realized (from fills) and unrealized.

    This is the case the lot-ledger-only approach misses: the LotLedger only
    sets `exit_price` when a lot is fully closed, so partial-exit prices are
    lost. compute_daily_pnl avoids that by walking fills directly.
    """
    lots = LotLedger()
    store = OMSStore(tmp_path / "oms.db")
    _seed(lots, store, _fill(AgentId.SONNET, "NVDA", OrderSide.BUY, "10", "100"))
    _seed(lots, store, _fill(AgentId.SONNET, "NVDA", OrderSide.SELL, "4", "120"))
    market = _StubMarket(closes={"NVDA": Decimal("130")})

    pnl = compute_daily_pnl(lots, store, market)

    # Realized on the 4 sold: 4 * (120 - 100) = 80
    assert pnl[AgentId.SONNET].realized == Decimal("80")
    # Unrealized on the 6 still open: 6 * (130 - 100) = 180
    assert pnl[AgentId.SONNET].unrealized == Decimal("180")
    assert pnl[AgentId.SONNET].total == Decimal("260")
    assert pnl[AgentId.SONNET].num_open_lots == 1
    assert pnl[AgentId.SONNET].num_closed_lots == 0
    store.close()


def test_attribution_returns_all_three_agents_even_when_empty(tmp_path: Path) -> None:
    """Stable-shape contract: every sleeve agent gets a row, zeros if no fills."""
    lots = LotLedger()
    store = OMSStore(tmp_path / "oms.db")
    market = _StubMarket(closes={})

    pnl = compute_daily_pnl(lots, store, market)

    assert set(pnl.keys()) == {AgentId.HAIKU, AgentId.SONNET, AgentId.OPUS}
    for br in pnl.values():
        assert br.realized == Decimal("0")
        assert br.unrealized == Decimal("0")
        assert br.total == Decimal("0")
        assert br.num_open_lots == 0
        assert br.num_closed_lots == 0
    store.close()


def test_attribution_missing_mark_zeros_unrealized(tmp_path: Path) -> None:
    """A held symbol with no available bar contributes 0 unrealized (no crash)."""
    lots = LotLedger()
    store = OMSStore(tmp_path / "oms.db")
    _seed(lots, store, _fill(AgentId.OPUS, "OBSCURE", OrderSide.BUY, "5", "50"))
    market = _StubMarket(closes={})  # no bars for any symbol

    pnl = compute_daily_pnl(lots, store, market)

    assert pnl[AgentId.OPUS].unrealized == Decimal("0")
    assert pnl[AgentId.OPUS].num_open_lots == 1
    store.close()


def test_attribution_fifo_matches_multiple_buy_lots(tmp_path: Path) -> None:
    """Two BUYs at different prices, one SELL: FIFO consumes the older lot first."""
    lots = LotLedger()
    store = OMSStore(tmp_path / "oms.db")
    _seed(lots, store, _fill(AgentId.HAIKU, "SPY", OrderSide.BUY, "5", "400"))
    _seed(lots, store, _fill(AgentId.HAIKU, "SPY", OrderSide.BUY, "5", "420"))
    # Sell 7 @ 430 — consumes 5 from the first BUY, then 2 from the second.
    # Realized: 5 * (430 - 400) + 2 * (430 - 420) = 150 + 20 = 170
    _seed(lots, store, _fill(AgentId.HAIKU, "SPY", OrderSide.SELL, "7", "430"))
    market = _StubMarket(closes={"SPY": Decimal("440")})

    pnl = compute_daily_pnl(lots, store, market)

    assert pnl[AgentId.HAIKU].realized == Decimal("170")
    # 3 shares of the second lot (@ 420) still open: 3 * (440 - 420) = 60
    assert pnl[AgentId.HAIKU].unrealized == Decimal("60")
    store.close()


# ── AgentPnLStore round-trip ──────────────────────────────────────────────────


def test_agent_pnl_store_roundtrip(tmp_path: object) -> None:
    db = tmp_path / "snapshots.db"  # type: ignore[operator]
    store = AgentPnLStore(db_path=db)

    br_haiku = PnLBreakdown(
        realized=Decimal("100"), unrealized=Decimal("50"),
        total=Decimal("150"), num_open_lots=2, num_closed_lots=1,
    )
    br_opus = PnLBreakdown(
        realized=Decimal("0"), unrealized=Decimal("-25"),
        total=Decimal("-25"), num_open_lots=1, num_closed_lots=0,
    )
    store.write_all(date(2026, 5, 11), {AgentId.HAIKU: br_haiku, AgentId.OPUS: br_opus})

    rows = store.recent(limit=10)
    by_agent = {r.agent_id: r for r in rows}

    assert by_agent[AgentId.HAIKU].realized == Decimal("100")
    assert by_agent[AgentId.HAIKU].total == Decimal("150")
    assert by_agent[AgentId.OPUS].unrealized == Decimal("-25")
    assert by_agent[AgentId.OPUS].num_closed == 0


def test_agent_pnl_store_upsert_replaces_same_day(tmp_path: object) -> None:
    """Same-day re-runs (e.g. crash recovery) update in place via PK."""
    db = tmp_path / "snapshots.db"  # type: ignore[operator]
    store = AgentPnLStore(db_path=db)
    snap_date = date(2026, 5, 11)

    first = PnLBreakdown(
        realized=Decimal("10"), unrealized=Decimal("0"),
        total=Decimal("10"), num_open_lots=0, num_closed_lots=1,
    )
    revised = PnLBreakdown(
        realized=Decimal("15"), unrealized=Decimal("5"),
        total=Decimal("20"), num_open_lots=1, num_closed_lots=1,
    )

    store.upsert_snapshot(snap_date, AgentId.HAIKU, first)
    store.upsert_snapshot(snap_date, AgentId.HAIKU, revised)

    rows = store.recent(agent_id=AgentId.HAIKU, limit=10)
    assert len(rows) == 1  # not 2 — replaced in place
    assert rows[0].total == Decimal("20")
