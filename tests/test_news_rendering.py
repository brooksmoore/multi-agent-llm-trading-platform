"""Tests for the news-rendering helpers used by agents and the manager.

Regression coverage for the bug where format_news_block silently dropped
NewsItem.summary, leaving Haiku and Sonnet reading headlines only despite
~98% of items having populated summaries (avg ~200 chars from Finnhub).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from agents.base import AgentState, format_news_block
from core.types import (
    AgentId,
    KillSwitchState,
    NewsItem,
    NewsSource,
)
from execution.broker import BrokerAccount


_TS = datetime(2026, 5, 5, 14, 0, tzinfo=UTC)


def _news(
    headline: str = "Acme tops earnings",
    summary: str | None = None,
    symbols: tuple[str, ...] = ("ACME",),
    when: datetime = _TS,
    source: NewsSource = NewsSource.FINNHUB,
) -> NewsItem:
    return NewsItem(
        source=source,
        headline=headline,
        url=f"https://example.com/{headline}",
        published_at=when,
        symbols=symbols,
        summary=summary,
    )


def _state(news: list[NewsItem]) -> AgentState:
    return AgentState(
        timestamp=_TS,
        bars_by_symbol={},
        news=news,
        positions=[],
        account=BrokerAccount(
            cash=Decimal("100000"),
            equity=Decimal("100000"),
            buying_power=Decimal("200000"),
            pattern_day_trader=False,
            daytrade_count=0,
        ),
        kill_switch_state=KillSwitchState.OK,
        master_capability=Decimal("1.0"),
        effective_max_gross=Decimal("0.5"),
    )


def test_summary_is_included_in_block() -> None:
    """The fix: summary text must appear in rendered block, not just headline."""
    summary = "Acme reported Q1 EPS of $1.23 on revenue of $4.5B, beating consensus."
    block = format_news_block(_state([_news(summary=summary)]))
    assert "Acme tops earnings" in block
    assert summary in block


def test_summary_omitted_when_empty() -> None:
    """No summary line should be emitted when summary is None or whitespace."""
    block = format_news_block(_state([_news(summary=None)]))
    lines = block.splitlines()
    # Header line + 1 headline line, nothing else.
    assert len(lines) == 2
    block2 = format_news_block(_state([_news(summary="   ")]))
    assert len(block2.splitlines()) == 2


def test_summary_truncated_long() -> None:
    """Long summaries must be truncated to keep agent context bounded."""
    long_summary = "x" * 5000
    block = format_news_block(_state([_news(summary=long_summary)]))
    # No single line should exceed a reasonable bound (cap is 280 + indent).
    longest = max(len(line) for line in block.splitlines())
    assert longest < 320, f"summary line not truncated: {longest} chars"
    assert "..." in block


def test_no_news_renders_placeholder() -> None:
    block = format_news_block(_state([]))
    assert "no items" in block.lower()


def test_newest_first_and_limit_applies() -> None:
    items = [
        _news(headline=f"item-{i}", when=_TS.replace(hour=i)) for i in range(20)
    ]
    block = format_news_block(_state(items), limit=5)
    # Newest (hour=19) appears, oldest (hour=0) does not.
    assert "item-19" in block
    assert "item-0" not in block
