"""HaikuSynthesizer tests (T2.3). LLM is mocked; no live calls."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from agents.haiku_synthesizer import HaikuSynthesizer, positions_from_lot_ledger
from agents.llm import HAIKU_MODEL, LLMClient
from agents.manager_bridge import read_manager_context
from agents.memory import AgentMemory
from core.types import AgentId, AgentMemo, Fill, NewsItem, NewsSource, OrderSide, VixBucket, new_id
from data.news_store import NewsStore
from execution.lots import LotLedger
from ops.agent_pnl_store import AgentPnLStore
from ops.attribution import PnLBreakdown

_TS = datetime(2026, 5, 11, 13, 30, tzinfo=UTC)


def _memo() -> AgentMemo:
    return AgentMemo(
        id=new_id(), agent_id=AgentId.HAIKU, call_type="morning_synthesis",
        model=HAIKU_MODEL, timestamp=_TS,
        cached_tokens=0, new_input_tokens=100, output_tokens=200,
        cost_usd=Decimal("0.005"), prompt_hash="x",
        response_json="brief", intents_emitted=0,
    )


def _llm_returning(text: str) -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.call.return_value = (text, _memo())
    return client


def _make_news(tmp_path: Path, *, with_scored: bool = True) -> NewsStore:
    store = NewsStore(tmp_path / "news.db")
    if with_scored:
        item = NewsItem(
            source=NewsSource.YFINANCE,
            headline="NVDA blockbuster earnings beat",
            url="http://example/nvda-q2",
            published_at=_TS - timedelta(hours=4),
            symbols=("NVDA",),
            summary="...",
            sentiment=None,
            body="long enough body" * 30,
        )
        store.add_items([item])
        store.update_score(
            url=item.url, impact=5,
            affected_symbols=("NVDA",), surprise="high",
            scored_at=_TS - timedelta(hours=3),
        )
    return store


def _seed_pnl(tmp_path: Path) -> Path:
    """Write a week of agent_pnl_daily rows so the synthesizer has P&L to read."""
    db = tmp_path / "snapshots.db"
    store = AgentPnLStore(db_path=db)
    base_date = (_TS - timedelta(days=2)).date()
    breakdowns = {
        AgentId.HAIKU: PnLBreakdown(
            realized=Decimal("8.40"), unrealized=Decimal("0"),
            total=Decimal("8.40"), num_open_lots=2, num_closed_lots=1,
        ),
        AgentId.SONNET: PnLBreakdown(
            realized=Decimal("23.10"), unrealized=Decimal("5"),
            total=Decimal("28.10"), num_open_lots=4, num_closed_lots=2,
        ),
        AgentId.OPUS: PnLBreakdown(
            realized=Decimal("0"), unrealized=Decimal("12"),
            total=Decimal("12"), num_open_lots=3, num_closed_lots=0,
        ),
    }
    store.write_all(base_date, breakdowns)
    return db


# ── Happy path ────────────────────────────────────────────────────────────────


def test_synthesize_persists_brief_and_returns_text(tmp_path: Path) -> None:
    news_store = _make_news(tmp_path, with_scored=True)
    db = _seed_pnl(tmp_path)
    mem = AgentMemory(":memory:", AgentId.MANAGER)

    expected_brief = "**Macro pulse**\n\nVIX 16, SWEET_SPOT. " + "x" * 100
    llm = _llm_returning(expected_brief)
    synth = HaikuSynthesizer(
        llm=llm, manager_memory=mem,
        news_store=news_store, snapshot_db_path=db,
    )

    result = synth.synthesize(
        positions_by_agent={
            AgentId.HAIKU: [("SPY", Decimal("10"))],
            AgentId.SONNET: [],
            AgentId.OPUS: [("TSM", Decimal("5"))],
        },
        vix_value=Decimal("16"),
        vix_bucket=VixBucket.SWEET_SPOT,
        now=_TS,
    )

    assert result is not None
    assert result.startswith("**Macro pulse**")
    # The manager_memory now has the persisted brief readable by sleeve agents.
    ctx = read_manager_context(mem, AgentId.HAIKU)
    assert ctx.get("morning_brief", "").startswith("**Macro pulse**")
    mem.close()


def test_synthesize_user_message_includes_all_four_input_blocks(tmp_path: Path) -> None:
    news_store = _make_news(tmp_path, with_scored=True)
    db = _seed_pnl(tmp_path)
    mem = AgentMemory(":memory:", AgentId.MANAGER)
    llm = _llm_returning("**Macro pulse**\nbrief")
    synth = HaikuSynthesizer(
        llm=llm, manager_memory=mem,
        news_store=news_store, snapshot_db_path=db,
    )

    synth.synthesize(
        positions_by_agent={AgentId.HAIKU: [("SPY", Decimal("10"))]},
        vix_value=Decimal("16"),
        vix_bucket=VixBucket.SWEET_SPOT,
        now=_TS,
    )

    user_msg = llm.call.call_args.kwargs["user"]
    # All four blocks must be present
    assert "VIX:" in user_msg
    assert "Holdings snapshot" in user_msg
    assert "per-sleeve P&L" in user_msg
    assert "Top news" in user_msg
    # And the top-impact news item bubbled up
    assert "NVDA" in user_msg
    mem.close()


def test_synthesize_handles_no_scored_news(tmp_path: Path) -> None:
    news_store = _make_news(tmp_path, with_scored=False)
    db = _seed_pnl(tmp_path)
    mem = AgentMemory(":memory:", AgentId.MANAGER)
    llm = _llm_returning("**Macro pulse**\nquiet day brief")
    synth = HaikuSynthesizer(
        llm=llm, manager_memory=mem,
        news_store=news_store, snapshot_db_path=db,
    )

    result = synth.synthesize(
        positions_by_agent={},
        vix_value=Decimal("11"),
        vix_bucket=VixBucket.VERY_LOW,
        now=_TS,
    )

    assert result is not None
    user_msg = llm.call.call_args.kwargs["user"]
    assert "no items met threshold" in user_msg
    mem.close()


def test_synthesize_handles_missing_pnl_snapshots(tmp_path: Path) -> None:
    news_store = _make_news(tmp_path, with_scored=True)
    # snapshot_db doesn't exist
    db = tmp_path / "nonexistent.db"
    mem = AgentMemory(":memory:", AgentId.MANAGER)
    llm = _llm_returning("**Macro pulse**\nbrief")
    synth = HaikuSynthesizer(
        llm=llm, manager_memory=mem,
        news_store=news_store, snapshot_db_path=db,
    )

    result = synth.synthesize(
        positions_by_agent={},
        vix_value=None, vix_bucket=None, now=_TS,
    )

    assert result is not None
    user_msg = llm.call.call_args.kwargs["user"]
    # Each sleeve should still appear with "no snapshots yet"
    assert "no snapshots yet" in user_msg
    mem.close()


def test_synthesize_empty_llm_response_does_not_persist(tmp_path: Path) -> None:
    news_store = _make_news(tmp_path)
    db = _seed_pnl(tmp_path)
    mem = AgentMemory(":memory:", AgentId.MANAGER)
    llm = _llm_returning("   ")  # whitespace-only
    synth = HaikuSynthesizer(
        llm=llm, manager_memory=mem,
        news_store=news_store, snapshot_db_path=db,
    )

    result = synth.synthesize(
        positions_by_agent={},
        vix_value=Decimal("16"), vix_bucket=VixBucket.SWEET_SPOT, now=_TS,
    )

    assert result is None
    # And the manager_memory has no morning brief recorded.
    ctx = read_manager_context(mem, AgentId.HAIKU)
    assert not ctx.get("morning_brief", "")
    mem.close()


# ── positions_from_lot_ledger ─────────────────────────────────────────────────


def test_positions_from_lot_ledger_aggregates_per_agent() -> None:
    lots = LotLedger()
    # 2 fills for HAIKU on SPY, 1 for OPUS on TSM, none for SONNET
    lots.book_fill(Fill(
        id=new_id(), order_id=new_id(), agent_id=AgentId.HAIKU,
        symbol="SPY", side=OrderSide.BUY,
        qty=Decimal("5"), price=Decimal("400"),
        timestamp=_TS,
    ))
    lots.book_fill(Fill(
        id=new_id(), order_id=new_id(), agent_id=AgentId.HAIKU,
        symbol="SPY", side=OrderSide.BUY,
        qty=Decimal("3"), price=Decimal("410"),
        timestamp=_TS,
    ))
    lots.book_fill(Fill(
        id=new_id(), order_id=new_id(), agent_id=AgentId.OPUS,
        symbol="TSM", side=OrderSide.BUY,
        qty=Decimal("2"), price=Decimal("200"),
        timestamp=_TS,
    ))

    out = positions_from_lot_ledger(lots)

    assert out[AgentId.HAIKU] == [("SPY", Decimal("8"))]
    assert out[AgentId.SONNET] == []
    assert out[AgentId.OPUS] == [("TSM", Decimal("2"))]
