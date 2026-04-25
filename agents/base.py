"""BaseAgent ABC and AgentState snapshot."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.types import AgentId, Intent, KillSwitchState, NewsItem
from data.market import Bar
from execution.broker import BrokerAccount, BrokerPosition


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


class BaseAgent(ABC):
    def __init__(self, agent_id: AgentId) -> None:
        self._agent_id = agent_id

    @property
    def agent_id(self) -> AgentId:
        return self._agent_id

    @abstractmethod
    def observe(self, state: AgentState) -> list[Intent]:
        """Process system state snapshot, return zero or more trade intents."""
