"""Deterministic Python briefing summarizer — token-capped market + news context for LLMs."""

from __future__ import annotations

from core.types import NewsItem
from data.market import Bar

_CHARS_PER_TOKEN = 4


class BriefingSummarizer:
    def summarize_bars(
        self, bars: list[Bar], symbol: str, max_chars: int = 2000
    ) -> str:
        reversed_bars = list(reversed(bars))
        header = f"=== {symbol} price history (last {len(reversed_bars)} bars) ===\n"
        lines: list[str] = [header]
        total = len(header)
        truncated = False
        for bar in reversed_bars:
            line = (
                f"  {bar.timestamp.strftime('%Y-%m-%d')}"
                f"  O:{bar.open:.2f}"
                f" H:{bar.high:.2f}"
                f" L:{bar.low:.2f}"
                f" C:{bar.close:.2f}"
                f" V:{bar.volume:,}"
            )
            if bar.vwap is not None:
                line += f" VWAP:{bar.vwap:.2f}"
            line += "\n"
            if total + len(line) > max_chars:
                truncated = True
                break
            lines.append(line)
            total += len(line)
        if truncated:
            lines.append("  ... (truncated)\n")
        return "".join(lines)

    def summarize_news(
        self, items: list[NewsItem], max_chars: int = 2000
    ) -> str:
        sorted_items = sorted(items, key=lambda x: x.published_at, reverse=True)
        header = f"=== News ({len(items)} items) ===\n"
        lines: list[str] = [header]
        total = len(header)
        for i, item in enumerate(sorted_items):
            sentiment_str = ""
            if item.sentiment is not None:
                sentiment_str = f" (sentiment: {item.sentiment:+.2f})"
            line = (
                f"  {i + 1}. [{item.source.upper()}]"
                f" {item.published_at.strftime('%Y-%m-%d')}"
                f" — {item.headline}{sentiment_str}\n"
            )
            summary_line = ""
            if item.summary:
                summary_line = f"     {item.summary[:200]}\n"
            block = line + summary_line
            if total + len(block) > max_chars:
                break
            lines.append(block)
            total += len(block)
        return "".join(lines)

    def build_market_brief(
        self,
        bars_by_symbol: dict[str, list[Bar]],
        news: list[NewsItem],
        max_chars: int = 6000,
    ) -> str:
        bars_budget = int(max_chars * 0.6)
        news_budget = int(max_chars * 0.4)
        sorted_symbols = sorted(bars_by_symbol.keys())
        per_symbol_budget = (
            bars_budget // len(sorted_symbols) if sorted_symbols else bars_budget
        )
        bars_parts: list[str] = []
        for symbol in sorted_symbols:
            bars_parts.append(
                self.summarize_bars(bars_by_symbol[symbol], symbol, per_symbol_budget)
            )
        bars_section = "\n".join(bars_parts)
        news_section = self.summarize_news(news, news_budget)
        return bars_section + "\n" + news_section
