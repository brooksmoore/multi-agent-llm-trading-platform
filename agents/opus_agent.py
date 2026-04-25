"""OpusAgent: Concentrated GARP discretionary PM with scheduled deep-dives."""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

from agents.base import AgentState, BaseAgent
from agents.llm import LLMClient
from agents.memory import AgentMemory
from core.types import Action, AgentId, Intent, KillSwitchState, Sleeve, new_id

_log = logging.getLogger(__name__)

_MAX_INTENTS = 3
_PROMPT_PATH = Path(__file__).parent / "prompts" / "opus_agent.md"

# "hold" maps to None (no order emitted); others map to core Action strings
_ACTION_MAP: dict[str, str | None] = {
    "trim": "sell",
    "add": "buy",
    "rebalance_to": "rebalance_to",
    "exit": "sell",
    "hold": None,
}


class OpusAgent(BaseAgent):
    def __init__(self, llm: LLMClient, memory: AgentMemory) -> None:
        super().__init__(AgentId.OPUS)
        self._llm = llm
        self._memory = memory
        self._prompt = _PROMPT_PATH.read_text()

    def observe(self, state: AgentState) -> list[Intent]:
        """Daily thesis health check; return ≤3 intents."""
        if state.kill_switch_state == KillSwitchState.DRAWDOWN_LIQUIDATE:
            _log.warning("kill switch DRAWDOWN_LIQUIDATE — opus skipping cycle")
            return []

        context = self._format_daily_context(state)

        try:
            response_text, _ = self._llm.call(
                system=self._prompt,
                user=context,
                agent_id=AgentId.OPUS,
                call_type="daily_check",
                max_tokens=2048,
            )
        except Exception:
            _log.warning("LLM call failed in OpusAgent.observe", exc_info=True)
            return []

        intents = self._parse_daily_intents(response_text, state)

        for intent in intents:
            self._memory.record_intent(
                intent_id=intent.id,
                symbol=intent.symbol,
                action=str(intent.action),
                conviction=intent.conviction,
                rationale=intent.rationale,
                ts=intent.timestamp,
            )

        return intents

    def deep_dive(self, state: AgentState, symbol: str, doc_pack: str) -> dict[str, Any]:
        """Run a full Thursday/Friday deep-dive for one holding."""
        user_msg = "\n".join([
            f"DEEP DIVE TARGET: {symbol}",
            f"Current portfolio equity: {float(state.account.equity):.2f}",
            "",
            doc_pack,
        ])

        try:
            response_text, _ = self._llm.call(
                system=self._prompt,
                user=user_msg,
                agent_id=AgentId.OPUS,
                call_type="deep_dive",
                max_tokens=4096,
            )
        except Exception:
            _log.warning("OpusAgent.deep_dive failed for %s", symbol, exc_info=True)
            return {}

        return self._parse_json(response_text, context=f"deep_dive({symbol})")

    # ── Private helpers ────────────────────────────────────────────────────────

    def _format_daily_context(self, state: AgentState) -> str:
        positions_str = (
            ", ".join(f"{p.symbol}:{p.qty}" for p in state.positions)
            if state.positions
            else "flat"
        )
        regime = state.manager_regime_text or "(none this week)"
        critique = state.manager_critique or "(none)"
        recent = self._memory.recent_intents_summary(3)

        return "\n".join([
            f"=== OpusAgent daily check @ {state.timestamp.isoformat()} ===",
            "",
            f"Holdings          : {positions_str}",
            f"Cash              : {float(state.account.cash):.2f}",
            f"Effective max gross: {float(state.effective_max_gross):.2f}",
            "",
            f"Manager regime: {regime}",
            f"Manager critique: {critique}",
            "",
            f"Recent intents:\n{recent}",
            "",
            "Today's question: Review all active theses. Has anything broken today? "
            "Any catalyst in the next 5 days requiring position adjustment? "
            "Return JSON only.",
        ])

    def _parse_daily_intents(self, response_text: str, state: AgentState) -> list[Intent]:
        data = self._parse_json(response_text, context="daily_check")
        if not data:
            return []

        obs = str(data.get("portfolio_observation", ""))
        intents: list[Intent] = []

        for item in data.get("intents", [])[: _MAX_INTENTS]:
            try:
                raw_action = str(item.get("action", "buy"))
                mapped = _ACTION_MAP.get(raw_action, raw_action)
                if mapped is None:
                    continue  # "hold" → no order
                action = Action(mapped)
                intent = Intent(
                    id=new_id(),
                    agent_id=AgentId.OPUS,
                    symbol=str(item["symbol"]),
                    action=action,
                    target_weight=Decimal(str(item.get("target_weight", 0))),
                    sleeve=Sleeve.EQUITY,
                    signal=str(item.get("trigger", item.get("thesis_id", "")))[:140],
                    conviction=max(1, min(10, int(item.get("conviction", 5)))),
                    rationale=str(item.get("trigger", ""))[:280],
                    timestamp=state.timestamp,
                    regime_observation=obs[:200],
                )
                intents.append(intent)
            except (KeyError, ValueError):
                _log.warning("OpusAgent: skipping malformed intent", exc_info=True)

        return intents

    def _parse_json(self, text: str, context: str = "") -> dict[str, Any]:
        try:
            result: dict[str, Any] = json.loads(text)
            return result
        except json.JSONDecodeError:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end > start:
                try:
                    extracted: dict[str, Any] = json.loads(text[start : end + 1])
                    return extracted
                except json.JSONDecodeError:
                    pass
            _log.warning("OpusAgent: could not parse JSON in %s", context)
            return {}
