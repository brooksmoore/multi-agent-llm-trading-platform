"""NewsScorer tests (T2.2). LLM is mocked; no live calls."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from agents.llm import HAIKU_MODEL, LLMClient
from agents.news_scorer import NewsScorer
from core.events import EventBus, NewsHighImpactScoredEvent
from core.types import AgentId, AgentMemo, NewsItem, NewsSource, new_id
from data.news_store import NewsStore

_TS = datetime(2026, 5, 11, 14, 0, tzinfo=UTC)
_LONG_BODY = (
    "The company reported strong quarterly earnings significantly above consensus, "
    "with the CFO citing favorable demand in the AI accelerator segment and a "
    "guide-up for the next quarter that exceeded sell-side expectations. Several "
    "analysts have already published rating upgrades in response."
)


def _memo() -> AgentMemo:
    return AgentMemo(
        id=new_id(), agent_id=AgentId.HAIKU, call_type="news_impact_score",
        model=HAIKU_MODEL, timestamp=_TS,
        cached_tokens=0, new_input_tokens=10, output_tokens=20,
        cost_usd=Decimal("0.0001"), prompt_hash="x",
        response_json="{}", intents_emitted=0,
    )


def _llm_returning(response_json: str) -> MagicMock:
    client = MagicMock(spec=LLMClient)
    client.call.return_value = (response_json, _memo())
    return client


def _item(symbols: tuple[str, ...] = ("NVDA",), body: str | None = _LONG_BODY) -> NewsItem:
    return NewsItem(
        source=NewsSource.YFINANCE,
        headline="NVDA reports Q2 earnings",
        url=f"http://example/{new_id()}",
        published_at=_TS,
        symbols=symbols,
        summary="(short summary)",
        sentiment=None,
        body=body,
    )


def _store(tmp_path: Path) -> NewsStore:
    return NewsStore(tmp_path / "news.db")


# ── Pre-filter ────────────────────────────────────────────────────────────────


def test_score_skips_item_without_body(tmp_path: Path) -> None:
    llm = _llm_returning('{"impact":3,"affected_symbols":["NVDA"],"surprise":"low"}')
    scorer = NewsScorer(llm=llm, store=_store(tmp_path))

    result = scorer.score(_item(body=None))

    assert result is None
    llm.call.assert_not_called()


def test_score_skips_item_with_short_body(tmp_path: Path) -> None:
    llm = _llm_returning('{"impact":3,"affected_symbols":["NVDA"],"surprise":"low"}')
    scorer = NewsScorer(llm=llm, store=_store(tmp_path))

    result = scorer.score(_item(body="too short to score"))

    assert result is None
    llm.call.assert_not_called()


def test_score_skips_item_with_no_in_universe_symbol(tmp_path: Path) -> None:
    """SYM not in PLUMBING_UNIVERSE: skip (we can't trade it anyway)."""
    llm = _llm_returning('{"impact":3,"affected_symbols":["ZZZZ"],"surprise":"low"}')
    scorer = NewsScorer(llm=llm, store=_store(tmp_path))

    result = scorer.score(_item(symbols=("ZZZZ",)))

    assert result is None
    llm.call.assert_not_called()


# ── Happy path + persistence ──────────────────────────────────────────────────


def test_score_persists_to_store_and_returns_parsed_score(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = _item()
    store.add_items([item])

    llm = _llm_returning(
        '{"impact":3,"affected_symbols":["NVDA","AMD"],"surprise":"med"}'
    )
    scorer = NewsScorer(llm=llm, store=store)

    result = scorer.score(item)

    assert result == {"impact": 3, "affected_symbols": ["NVDA", "AMD"], "surprise": "med"}
    llm.call.assert_called_once()
    # Verify persistence: unscored_recent should no longer return this item.
    unscored = store.unscored_recent(since=datetime(2026, 5, 1, tzinfo=UTC))
    assert all(i.url != item.url for i in unscored)


def test_score_publishes_event_when_impact_geq_4(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = _item()
    store.add_items([item])

    bus = EventBus()
    received: list[NewsHighImpactScoredEvent] = []
    bus.subscribe(
        "news.high_impact_scored",
        lambda e: received.append(e),  # type: ignore[arg-type]
    )

    llm = _llm_returning(
        '{"impact":4,"affected_symbols":["NVDA"],"surprise":"high"}'
    )
    scorer = NewsScorer(llm=llm, store=store, bus=bus)
    scorer.score(item)

    assert len(received) == 1
    assert received[0].symbol == "NVDA"
    assert received[0].impact == 4


def test_score_does_not_publish_event_when_impact_lt_4(tmp_path: Path) -> None:
    store = _store(tmp_path)
    item = _item()
    store.add_items([item])

    bus = EventBus()
    received: list[NewsHighImpactScoredEvent] = []
    bus.subscribe(
        "news.high_impact_scored",
        lambda e: received.append(e),  # type: ignore[arg-type]
    )

    llm = _llm_returning(
        '{"impact":3,"affected_symbols":["NVDA"],"surprise":"low"}'
    )
    scorer = NewsScorer(llm=llm, store=store, bus=bus)
    scorer.score(item)

    assert received == []


# ── Robustness ────────────────────────────────────────────────────────────────


def test_score_tolerates_markdown_fence_around_json(tmp_path: Path) -> None:
    """The Haiku response often comes wrapped in ```json fences; we should parse."""
    fenced = '```json\n{"impact":2,"affected_symbols":["NVDA"],"surprise":"low"}\n```'
    llm = _llm_returning(fenced)
    scorer = NewsScorer(llm=llm, store=_store(tmp_path))

    result = scorer.score(_item())

    assert result is not None
    assert result["impact"] == 2


def test_score_returns_none_on_unparseable_response(tmp_path: Path) -> None:
    llm = _llm_returning("totally not json")
    scorer = NewsScorer(llm=llm, store=_store(tmp_path))

    assert scorer.score(_item()) is None


def test_score_returns_none_on_schema_violation(tmp_path: Path) -> None:
    """impact=7 is out of [1,5]; reject the whole response."""
    llm = _llm_returning('{"impact":7,"affected_symbols":["NVDA"],"surprise":"high"}')
    scorer = NewsScorer(llm=llm, store=_store(tmp_path))

    assert scorer.score(_item()) is None


def test_score_returns_none_on_invalid_surprise(tmp_path: Path) -> None:
    """surprise='medium' (vs. required 'med') fails strict validation."""
    llm = _llm_returning('{"impact":3,"affected_symbols":["NVDA"],"surprise":"medium"}')
    scorer = NewsScorer(llm=llm, store=_store(tmp_path))

    assert scorer.score(_item()) is None


# ── NewsStore migration columns are present + queryable ───────────────────────


def test_unscored_recent_filters_by_body_length_and_scored_at(tmp_path: Path) -> None:
    store = _store(tmp_path)
    a = _item(body=_LONG_BODY)
    b = _item(body=None)
    c = _item(body=_LONG_BODY)
    store.add_items([a, b, c])
    # Mark c as already scored.
    store.update_score(
        url=c.url, impact=2, affected_symbols=("NVDA",),
        surprise="low", scored_at=datetime.now(UTC),
    )

    out = store.unscored_recent(
        since=datetime(2026, 5, 1, tzinfo=UTC), limit=10, min_body_chars=200,
    )
    urls = {i.url for i in out}

    assert a.url in urls      # has body, not scored — included
    assert b.url not in urls  # no body — excluded
    assert c.url not in urls  # already scored — excluded


# ── score_batch ───────────────────────────────────────────────────────────────


def test_score_batch_counts_successful_scores(tmp_path: Path) -> None:
    store = _store(tmp_path)
    good = _item()
    short = _item(body="x")
    store.add_items([good, short])

    llm = _llm_returning('{"impact":2,"affected_symbols":["NVDA"],"surprise":"low"}')
    scorer = NewsScorer(llm=llm, store=store)

    n = scorer.score_batch([good, short])
    assert n == 1
    # The short item should never have reached the LLM
    assert llm.call.call_count == 1
