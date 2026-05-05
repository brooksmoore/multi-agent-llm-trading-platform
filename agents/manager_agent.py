"""ManagerAgent: CIO-level capital allocation, risk oversight, regime reads, weekly journal."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from agents.base import AgentState
from agents.json_utils import parse_json_object
from agents.llm import BudgetExhausted, LLMClient
from agents.memory import AgentMemory
from core.types import AgentId, Intent
from ops.manager_analytics import ManagerContext

_log = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).parent / "prompts" / "manager_agent.md"


class ManagerAgent:
    """CIO orchestrator with six distinct call types, each with its own output schema."""

    def __init__(self, llm: LLMClient, memory: AgentMemory) -> None:
        self._llm = llm
        self._memory = memory
        self._prompt = _PROMPT_PATH.read_text()

    # ── Public call types ──────────────────────────────────────────────────────

    def regime_read(
        self,
        state: AgentState,
        prior_regime: str = "",
        ctx: ManagerContext | None = None,
    ) -> dict[str, Any]:
        """Friday regime read: macro snapshot → regime_read.json."""
        user_msg = self._regime_context(state, prior_regime, ctx)
        return self._call_and_parse("regime_read", user_msg)

    def adversarial_critique(
        self,
        state: AgentState,
        intents: list[Intent],
        ctx: ManagerContext | None = None,
    ) -> dict[str, Any]:
        """Adversarial red-team of high-conviction intents → critique.json."""
        lines: list[str] = [
            f"=== Adversarial critique @ {state.timestamp.isoformat()} ===",
            "",
        ]
        if ctx is not None:
            lines += [ctx.as_prompt_block(), ""]
        lines.append("Intents to critique:")
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
        self,
        state: AgentState,
        four_week_snapshot: str = "",
        ctx: ManagerContext | None = None,
    ) -> dict[str, Any]:
        """4-week Sortino-based sleeve reallocation → reallocation.json."""
        # Prefer the analytics block when provided; the legacy `four_week_snapshot`
        # string parameter is kept for backward-compatibility with callers.
        body = ctx.as_prompt_block() if ctx is not None else four_week_snapshot
        user_msg = "\n".join([
            f"=== Capital reallocation @ {state.timestamp.isoformat()} ===",
            "",
            body or "(no portfolio analytics available)",
            "",
            "Return reallocation.json only.",
        ])
        return self._call_and_parse("capital_reallocation", user_msg)

    def risk_check(
        self,
        state: AgentState,
        intent: Intent,
        ctx: ManagerContext | None = None,
    ) -> dict[str, Any]:
        """Pre-trade risk approval for a single intent → risk_check.json."""
        lines = [f"=== Risk check @ {state.timestamp.isoformat()} ===", ""]
        if ctx is not None:
            lines += [ctx.as_prompt_block(), ""]
        lines += [
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
        ]
        return self._call_and_parse("risk_check", "\n".join(lines))

    def drawdown_response(
        self,
        state: AgentState,
        drawdown_pct: float,
        attribution: dict[str, float],
        ctx: ManagerContext | None = None,
    ) -> dict[str, Any]:
        """Ad-hoc drawdown circuit breaker → drawdown_response.json."""
        attr_str = ", ".join(f"{k}: {v:.2%}" for k, v in attribution.items())
        lines = [f"=== Drawdown response @ {state.timestamp.isoformat()} ===", ""]
        if ctx is not None:
            lines += [ctx.as_prompt_block(), ""]
        lines += [
            f"Current drawdown  : {drawdown_pct:.2%}",
            f"Attribution       : {attr_str}",
            f"Kill switch state : {state.kill_switch_state}",
            "",
            "Return drawdown_response.json only.",
        ]
        return self._call_and_parse("drawdown_response", "\n".join(lines))

    def weekly_journal(
        self,
        state: AgentState,
        week_data: str = "",
        ctx: ManagerContext | None = None,
    ) -> str:
        """Friday end-of-week report → markdown string (≤1500 words)."""
        body = ctx.as_prompt_block() if ctx is not None else week_data
        user_msg = "\n".join([
            f"=== Weekly journal @ {state.timestamp.isoformat()} ===",
            "",
            body or "(no portfolio analytics available)",
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
        except BudgetExhausted:
            _log.warning("ManagerAgent.weekly_journal skipped: budget exhausted")
            return ""
        except Exception:
            _log.warning("ManagerAgent.weekly_journal LLM call failed", exc_info=True)
            return ""
        return response_text

    def master_capability_proposal(
        self,
        state: AgentState,
        evidence: dict[str, Any],
        ctx: ManagerContext | None = None,
    ) -> dict[str, Any]:
        """Propose a MASTER_CAPABILITY slider change → mc_proposal.json."""
        lines = [
            f"=== Master capability proposal @ {state.timestamp.isoformat()} ===",
            "",
        ]
        if ctx is not None:
            lines += [ctx.as_prompt_block(), ""]
        lines += [
            f"Evidence:\n{json.dumps(evidence, indent=2)}",
            f"Current master_capability: {float(state.master_capability):.2f}",
            "",
            "Return mc_proposal.json only.",
        ]
        return self._call_and_parse("master_capability_proposal", "\n".join(lines))

    # ── Private helpers ────────────────────────────────────────────────────────

    def _regime_context(
        self,
        state: AgentState,
        prior_regime: str,
        ctx: ManagerContext | None = None,
    ) -> str:
        vix_str = (
            f"VIX: {float(state.vix_value):.2f}" if state.vix_value else "VIX: n/a"
        )
        lines = [f"=== Regime read @ {state.timestamp.isoformat()} ===", ""]
        if ctx is not None:
            lines += [ctx.as_prompt_block(), ""]
        lines += [
            f"Portfolio equity  : {float(state.account.equity):.2f}",
            f"Kill switch       : {state.kill_switch_state}",
            vix_str,
            f"Master capability : {float(state.master_capability):.2f}",
            "",
            f"Prior regime: {prior_regime or '(none)'}",
            "",
            "Return regime_read.json only.",
        ]
        return "\n".join(lines)

    def _call_and_parse(self, call_type: str, user_msg: str) -> dict[str, Any]:
        try:
            response_text, _ = self._llm.call(
                system=self._prompt,
                user=user_msg,
                agent_id=AgentId.MANAGER,
                call_type=call_type,
                max_tokens=1536,
            )
        except BudgetExhausted:
            _log.warning("ManagerAgent.%s skipped: budget exhausted", call_type)
            return {}
        except Exception:
            _log.warning("ManagerAgent.%s LLM call failed", call_type, exc_info=True)
            return {}

        parsed = parse_json_object(response_text)
        if parsed is None:
            _log.warning("ManagerAgent.%s: could not parse JSON response", call_type)
            return {}
        return parsed
