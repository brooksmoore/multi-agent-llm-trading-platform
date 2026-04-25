"""ManagerAgent: CIO-level capital allocation, risk oversight, regime reads, weekly journal."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agents.base import AgentState
from agents.llm import LLMClient
from agents.memory import AgentMemory
from core.types import AgentId, Intent

_log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "manager_agent.md"


class ManagerAgent:
    """CIO orchestrator with six distinct call types, each with its own output schema."""

    def __init__(self, llm: LLMClient, memory: AgentMemory) -> None:
        self._llm = llm
        self._memory = memory
        self._prompt = _PROMPT_PATH.read_text()

    # ── Public call types ──────────────────────────────────────────────────────

    def regime_read(self, state: AgentState, prior_regime: str = "") -> dict[str, Any]:
        """Friday regime read: macro snapshot → regime_read.json."""
        user_msg = self._regime_context(state, prior_regime)
        return self._call_and_parse("regime_read", user_msg)

    def adversarial_critique(
        self, state: AgentState, intents: list[Intent]
    ) -> dict[str, Any]:
        """Adversarial red-team of high-conviction intents → critique.json."""
        lines: list[str] = [
            f"=== Adversarial critique @ {state.timestamp.isoformat()} ===",
            "",
            "Intents to critique:",
        ]
        for intent in intents:
            lines.append(
                f"  id={intent.id} agent={intent.agent_id} {intent.action}"
                f" {intent.symbol} weight={intent.target_weight}"
                f" conviction={intent.conviction}"
                f" rationale={intent.rationale[:120]}"
            )
        lines += ["", "Return critique.json only."]
        return self._call_and_parse("adversarial_critique", "\n".join(lines))

    def capital_reallocation(
        self, state: AgentState, four_week_snapshot: str
    ) -> dict[str, Any]:
        """4-week Sortino-based sleeve reallocation → reallocation.json."""
        user_msg = "\n".join([
            f"=== Capital reallocation @ {state.timestamp.isoformat()} ===",
            "",
            four_week_snapshot,
            "",
            "Return reallocation.json only.",
        ])
        return self._call_and_parse("capital_reallocation", user_msg)

    def risk_check(self, state: AgentState, intent: Intent) -> dict[str, Any]:
        """Pre-trade risk approval for a single intent → risk_check.json."""
        user_msg = "\n".join([
            f"=== Risk check @ {state.timestamp.isoformat()} ===",
            "",
            f"Intent: id={intent.id} agent={intent.agent_id}"
            f" {intent.action} {intent.symbol}"
            f" weight={intent.target_weight} conviction={intent.conviction}",
            f"Rationale: {intent.rationale}",
            "",
            f"Portfolio equity: {float(state.account.equity):.2f}",
            f"Kill switch: {state.kill_switch_state}",
            f"Master capability: {float(state.master_capability):.2f}",
            "",
            "Return risk_check.json only.",
        ])
        return self._call_and_parse("risk_check", user_msg)

    def drawdown_response(
        self,
        state: AgentState,
        drawdown_pct: float,
        attribution: dict[str, float],
    ) -> dict[str, Any]:
        """Ad-hoc drawdown circuit breaker → drawdown_response.json."""
        attr_str = ", ".join(f"{k}: {v:.2%}" for k, v in attribution.items())
        user_msg = "\n".join([
            f"=== Drawdown response @ {state.timestamp.isoformat()} ===",
            "",
            f"Current drawdown  : {drawdown_pct:.2%}",
            f"Attribution       : {attr_str}",
            f"Kill switch state : {state.kill_switch_state}",
            "",
            "Return drawdown_response.json only.",
        ])
        return self._call_and_parse("drawdown_response", user_msg)

    def weekly_journal(self, state: AgentState, week_data: str) -> str:
        """Friday end-of-week report → markdown string (≤1500 words)."""
        user_msg = "\n".join([
            f"=== Weekly journal @ {state.timestamp.isoformat()} ===",
            "",
            week_data,
            "",
            "Return the weekly journal in markdown. Maximum 1500 words.",
        ])
        try:
            response_text, _ = self._llm.call(
                system=self._prompt,
                user=user_msg,
                agent_id=AgentId.MANAGER,
                call_type="weekly_journal",
                max_tokens=2048,
            )
        except Exception:
            _log.warning("ManagerAgent.weekly_journal LLM call failed", exc_info=True)
            return ""
        return response_text

    def master_capability_proposal(
        self, state: AgentState, evidence: dict[str, Any]
    ) -> dict[str, Any]:
        """Propose a MASTER_CAPABILITY slider change → mc_proposal.json."""
        user_msg = "\n".join([
            f"=== Master capability proposal @ {state.timestamp.isoformat()} ===",
            "",
            f"Evidence:\n{json.dumps(evidence, indent=2)}",
            f"Current master_capability: {float(state.master_capability):.2f}",
            "",
            "Return mc_proposal.json only.",
        ])
        return self._call_and_parse("master_capability_proposal", user_msg)

    # ── Private helpers ────────────────────────────────────────────────────────

    def _regime_context(self, state: AgentState, prior_regime: str) -> str:
        vix_str = (
            f"VIX: {float(state.vix_value):.2f}" if state.vix_value else "VIX: n/a"
        )
        return "\n".join([
            f"=== Regime read @ {state.timestamp.isoformat()} ===",
            "",
            f"Portfolio equity  : {float(state.account.equity):.2f}",
            f"Kill switch       : {state.kill_switch_state}",
            vix_str,
            f"Master capability : {float(state.master_capability):.2f}",
            "",
            f"Prior regime: {prior_regime or '(none)'}",
            "",
            "Return regime_read.json only.",
        ])

    def _call_and_parse(self, call_type: str, user_msg: str) -> dict[str, Any]:
        try:
            response_text, _ = self._llm.call(
                system=self._prompt,
                user=user_msg,
                agent_id=AgentId.MANAGER,
                call_type=call_type,
                max_tokens=1536,
            )
        except Exception:
            _log.warning("ManagerAgent.%s LLM call failed", call_type, exc_info=True)
            return {}

        try:
            result: dict[str, Any] = json.loads(response_text)
            return result
        except json.JSONDecodeError:
            start = response_text.find("{")
            end = response_text.rfind("}")
            if start != -1 and end > start:
                try:
                    extracted: dict[str, Any] = json.loads(response_text[start : end + 1])
                    return extracted
                except json.JSONDecodeError:
                    pass
            _log.warning("ManagerAgent.%s: could not parse JSON response", call_type)
            return {}
