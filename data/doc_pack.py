"""Assemble the long-form research context (`doc_pack`) for Opus deep-dives.

Combines: 90 days of news, recent SEC filings, and a sector-context block
derived from news on a few sector-proxy ETFs. Output is plain text passed
verbatim to the Opus LLM call as the user message.

Designed to stay within Opus's prompt-cache window: nightly news pulls are
the same across deep-dives that day, so prompt caching at the Anthropic
side recovers most of the input cost on subsequent calls.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.types import NewsItem, NewsSource
from data.news_store import NewsStore


# Map a held single-name to a sector-proxy ETF for context.
# Conservative — only handles the names already in DEFAULT_UNIVERSE.
_SECTOR_PROXIES: dict[str, str] = {
    "AAPL":  "XLK",
    "NVDA":  "XLK",
    "MSFT":  "XLK",
    "GOOGL": "XLC",
    "META":  "XLC",
    "AMZN":  "XLY",
}


def _format_item(item: NewsItem) -> str:
    when = item.published_at.strftime("%Y-%m-%d %H:%M")
    summary = (item.summary or "").strip().replace("\n", " ")
    if len(summary) > 300:
        summary = summary[:297] + "..."
    body_note = ""
    if item.body:
        body_note = f"\n  body: {item.body[:1000]}"
    summary_line = f"\n  {summary}" if summary else ""
    return f"- [{when}] [{item.source.value}] {item.headline}{summary_line}{body_note}"


def build_doc_pack(
    symbol: str,
    store: NewsStore,
    *,
    now: datetime | None = None,
    company_lookback_days: int = 90,
    sector_lookback_days: int = 14,
    max_company_items: int = 40,
    max_sector_items: int = 10,
) -> str:
    """Build the long-form research context for one Opus deep-dive target.

    The pack is bounded so a single deep-dive stays under ~150K input tokens
    (well below Opus's window). Order: company news, filings, sector context.
    """
    now = now or datetime.now(UTC)

    company_since = now - timedelta(days=company_lookback_days)
    company_items = store.recent_for_symbols(
        [symbol], since=company_since, limit=max_company_items + 20
    )
    # Split filings vs. news — filings get their own header for clarity.
    filings = [i for i in company_items if i.source == NewsSource.EDGAR]
    news = [i for i in company_items if i.source != NewsSource.EDGAR][:max_company_items]

    sector_proxy = _SECTOR_PROXIES.get(symbol)
    sector_items: list[NewsItem] = []
    if sector_proxy is not None:
        sector_since = now - timedelta(days=sector_lookback_days)
        sector_items = store.recent_for_symbols(
            [sector_proxy], since=sector_since, limit=max_sector_items
        )

    blocks: list[str] = [
        f"=== Deep-dive doc_pack for {symbol} (assembled {now.isoformat()}) ===",
        "",
    ]

    if news:
        blocks.append(f"--- {symbol} news (last {company_lookback_days} days, {len(news)} items) ---")
        blocks.extend(_format_item(i) for i in news)
        blocks.append("")
    else:
        blocks.append(f"--- {symbol} news: no items in last {company_lookback_days} days ---")
        blocks.append("")

    if filings:
        blocks.append(f"--- {symbol} SEC filings (last {company_lookback_days} days, {len(filings)} items) ---")
        blocks.extend(_format_item(i) for i in filings)
        blocks.append("")
    else:
        blocks.append(f"--- {symbol} SEC filings: no 8-K or 10-Q in last {company_lookback_days} days ---")
        blocks.append("")

    if sector_proxy is not None:
        if sector_items:
            blocks.append(
                f"--- Sector context via {sector_proxy} "
                f"(last {sector_lookback_days} days, {len(sector_items)} items) ---"
            )
            blocks.extend(_format_item(i) for i in sector_items)
        else:
            blocks.append(f"--- Sector context via {sector_proxy}: no recent items ---")
        blocks.append("")

    blocks.append(
        "Reminders: weigh the news above against your written thesis. "
        "If the disconfirming evidence in `kill_criteria` is present, exit. "
        "Return the deep_dive JSON schema only."
    )
    return "\n".join(blocks)
