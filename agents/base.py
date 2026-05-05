"""BaseAgent ABC and AgentState snapshot."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.types import AgentId, Intent, KillSwitchState, NewsItem
from data.market import Bar
from execution.broker import BrokerAccount, BrokerPosition

_PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z_]+)\}\}")


def format_news_block(state: "AgentState", limit: int = 12) -> str:
    """Compact headline list for the agent's user-message context.

    Returns a multi-line block with one bullet per item, or a single
    "no recent items" line if state.news is empty. Newest first; capped.
    """
    if not state.news:
        return "Recent news: (no items in last 24h)"
    items = sorted(state.news, key=lambda n: n.published_at, reverse=True)[:limit]
    lines = [f"Recent news ({len(items)} of {len(state.news)} items, newest first):"]
    for n in items:
        when = n.published_at.strftime("%m-%d %H:%M")
        syms = ",".join(n.symbols) if n.symbols else "—"
        head = n.headline.strip().replace("\n", " ")
        if len(head) > 140:
            head = head[:137] + "..."
        lines.append(f"  [{when}] [{syms}] {head}")
    return "\n".join(lines)


def render_system_prompt(template: str, state: "AgentState") -> str:
    """Resolve placeholders in the system-prompt template.

    All placeholders (e.g. `{{effective_max_gross}}`) are replaced with a
    fixed marker pointing the model to the user-message context block. This
    keeps the system prompt byte-identical across calls so Anthropic prompt
    caching can hit (live numerics live in the user-message context block,
    where they're already shown to every agent).

    The `state` parameter is intentionally ignored; it's kept in the
    signature for callers that haven't been updated yet.
    """
    del state  # no longer used; values come from the user message
    return _PLACEHOLDER_RE.sub(lambda _m: "(see context block)", template)


@dataclass
class AgentState:
    """Full system snapshot passed to agent.observe() each cycle."""

    timestamp: datetime
    bars_by_symbol: dict[str, list[Bar]]
    news: list[NewsItem]
    positions: list[BrokerPosition]
    account: BrokerAccount
    kill_switch_state: KillSwitchState
    master_capability: Decimal
    effective_max_gross: Decimal
    vix_value: Decimal | None = None
    manager_regime_text: str = ""
    manager_critique: str = ""
    # New (M-bridge): populated from the Manager's daily morning brief
    # and any per-sleeve drawdown directive. Read by all sleeve agents
    # in their `_format_*_context` helpers.
    manager_morning_brief: str = ""
    manager_directive: str = ""
    # Recent orders that were rejected by the broker (last 48 h, this agent only).
    # Each entry: {"ts": ISO str, "symbol": str, "side": str, "reason": str}.
    # Populated by TradingApp.build_agent_state(); empty when no OMS store is
    # available (e.g. in unit tests that don't wire up OMSStore).
    recent_rejections: list[dict[str, str]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.recent_rejections is None:
            self.recent_rejections = []


class BaseAgent(ABC):
    def __init__(self, agent_id: AgentId) -> None:
        self._agent_id = agent_id

    @property
    def agent_id(self) -> AgentId:
        return self._agent_id

    @abstractmethod
    def observe(self, state: AgentState) -> list[Intent]:
        """Process system state snapshot, return zero or more trade intents."""

    def signal_fingerprint(self, state: "AgentState") -> str | None:
        """Hash of the inputs this agent keys on; used to skip no-op cycles.

        Return None to disable skipping (default — always run). Override in
        agents whose signals change rarely (trend-following, factor ranks)
        to avoid burning LLM calls on unchanged state.
        """
        return None
