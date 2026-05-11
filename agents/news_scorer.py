"""NewsScorer — Haiku-powered impact scoring for fresh news items (T2.2 / Plan 2c).

One LLM call per item; pre-filtered so we only spend on items with a
non-trivial body and at least one in-universe symbol. Score outputs
persist back onto news_items via NewsStore.update_score; items with
impact >= 4 also fire a NewsHighImpactScoredEvent on the EventBus for
T2.5 subscribers.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from agents.json_utils import parse_json_object
from agents.llm import BudgetExhausted, LLMClient
from config.universes import PLUMBING_UNIVERSE
from core.events import EventBus, NewsHighImpactScoredEvent
from core.types import AgentId, NewsItem
from data.news_store import NewsStore

log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "news_scorer.md"

# Pre-filter thresholds. Items not meeting these are silently skipped —
# scoring them is rarely worth the spend (no body = no causal claim to
# evaluate; bodies under 200 chars are essentially headlines; items with
# no in-universe symbol can't act on our trade book regardless of score).
_MIN_BODY_CHARS: int = 200
_UNIVERSE_SET: frozenset[str] = frozenset(s.upper() for s in PLUMBING_UNIVERSE)


def _should_score(item: NewsItem) -> bool:
    if item.body is None or len(item.body) < _MIN_BODY_CHARS:
        return False
    item_symbols = {s.upper() for s in item.symbols}
    return bool(item_symbols & _UNIVERSE_SET)


def _build_user_message(item: NewsItem) -> str:
    syms = ", ".join(item.symbols) if item.symbols else "(none)"
    body = item.body or ""
    return (
        f"Source:        {item.source.value}\n"
        f"Published:     {item.published_at.isoformat()}\n"
        f"Symbols:       {syms}\n"
        f"Headline:      {item.headline}\n\n"
        f"Body:\n{body}"
    )


def _coerce_score(parsed: dict[str, object]) -> tuple[int, tuple[str, ...], str] | None:
    """Validate parsed JSON and coerce to (impact, affected_symbols, surprise).

    Returns None when the response doesn't match the strict schema; the
    caller treats that as a failed score and skips persistence.
    """
    try:
        impact = int(parsed.get("impact"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if impact < 1 or impact > 5:
        return None
    raw_syms = parsed.get("affected_symbols")
    if not isinstance(raw_syms, list):
        return None
    syms = tuple(str(s).strip().upper() for s in raw_syms if str(s).strip())
    surprise = str(parsed.get("surprise", "")).lower()
    if surprise not in {"low", "med", "high"}:
        return None
    return impact, syms, surprise


class NewsScorer:
    """Per-item Haiku scoring. Pre-filters, calls LLM, persists, fires event on impact >= 4."""

    def __init__(
        self,
        llm: LLMClient,
        store: NewsStore,
        bus: EventBus | None = None,
    ) -> None:
        self._llm = llm
        self._store = store
        self._bus = bus
        self._prompt = _PROMPT_PATH.read_text()

    def score(self, item: NewsItem) -> dict[str, object] | None:
        """Score one item end-to-end. Returns the parsed dict, or None on skip/failure.

        Pre-filters out items that don't meet the minimum-body / in-universe
        bar — those silently return None without any LLM call.
        """
        if not _should_score(item):
            return None

        try:
            response_text, _ = self._llm.call(
                system=self._prompt,
                user=_build_user_message(item),
                agent_id=AgentId.HAIKU,
                call_type="news_impact_score",
                max_tokens=128,
            )
        except BudgetExhausted:
            log.warning("NewsScorer: budget exhausted; skipping %s", item.url)
            return None
        except Exception:
            log.warning("NewsScorer: LLM call failed for %s", item.url, exc_info=True)
            return None

        parsed = parse_json_object(response_text)
        if parsed is None:
            log.warning("NewsScorer: unparseable response for %s", item.url)
            return None

        coerced = _coerce_score(parsed)
        if coerced is None:
            log.warning("NewsScorer: response failed schema check for %s: %r", item.url, parsed)
            return None
        impact, affected, surprise = coerced

        scored_at = datetime.now(UTC)
        try:
            self._store.update_score(
                url=item.url, impact=impact,
                affected_symbols=affected, surprise=surprise,
                scored_at=scored_at,
            )
        except Exception:
            log.warning("NewsScorer: persist failed for %s", item.url, exc_info=True)
            # Keep going — the event is still worth firing if impact >= 4.

        if impact >= 4 and self._bus is not None:
            for sym in affected or (item.symbols[:1] if item.symbols else ()):
                try:
                    self._bus.publish(
                        NewsHighImpactScoredEvent(
                            symbol=sym,
                            impact=impact,
                            headline=item.headline,
                            published_at=item.published_at,
                        )
                    )
                except Exception:
                    log.warning("NewsScorer: bus.publish failed for %s", sym, exc_info=True)

        return {"impact": impact, "affected_symbols": list(affected), "surprise": surprise}

    def score_batch(self, items: Iterable[NewsItem]) -> int:
        """Score each item via score(); returns the number successfully scored."""
        scored = 0
        for item in items:
            if self.score(item) is not None:
                scored += 1
        return scored
