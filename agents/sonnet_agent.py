"""SonnetAgent: Multi-factor equity quant (value + momentum + quality Z-score composite)."""

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
from data.market import Bar

_log = logging.getLogger(__name__)

_MAX_INTENTS = 5
_MOMENTUM_LOOKBACK = 252   # ~12-month price momentum
_MOMENTUM_SKIP = 21        # skip last month (reversal filter)
_PROMPT_PATH = Path(__file__).parent / "prompts" / "sonnet_agent.md"

# Map non-standard action strings from the LLM schema to core Action values
_ACTION_MAP: dict[str, str] = {
    "trim": "sell",
    "add": "buy",
    "rebalance_to": "rebalance_to",
    "exit": "sell",
}


def _price_momentum(closes: list[Decimal], lookback: int, skip: int) -> Decimal | None:
    """12-1 momentum: return from closes[-(lookback+skip)] to closes[-skip]."""
    required = lookback + skip
    if len(closes) < required:
        return None
    entry = closes[-(lookback + skip)]
    exit_ = closes[-skip]
    if entry == Decimal("0"):
        return None
    return exit_ / entry - Decimal("1")


class SonnetAgent(BaseAgent):
    def __init__(self, llm: LLMClient, memory: AgentMemory) -> None:
        super().__init__(AgentId.SONNET)
        self._llm = llm
        self._memory = memory
        self._prompt = _PROMPT_PATH.read_text()

    def observe(self, state: AgentState) -> list[Intent]:
        """Evaluate factor signals, query LLM, return up to 5 intents."""
        if state.kill_switch_state == KillSwitchState.DRAWDOWN_LIQUIDATE:
            _log.warning("kill switch DRAWDOWN_LIQUIDATE — sonnet skipping cycle")
            return []

        factor_signals = self._compute_factor_signals(state.bars_by_symbol)
        context = self._format_context(state, factor_signals)

        try:
            response_text, _ = self._llm.call(
                system=self._prompt,
                user=context,
                agent_id=AgentId.SONNET,
                call_type="factor_observe",
                max_tokens=1536,
            )
        except Exception:
            _log.warning("LLM call failed in SonnetAgent.observe", exc_info=True)
            return []

        intents = self._parse_intents(response_text, state)

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

    def _compute_factor_signals(
        self, bars_by_symbol: dict[str, list[Bar]]
    ) -> dict[str, dict[str, Any]]:
        """Compute price momentum proxy for each symbol with sufficient history."""
        result: dict[str, dict[str, Any]] = {}
        for symbol, bars in bars_by_symbol.items():
            sorted_bars = sorted(bars, key=lambda b: b.timestamp)
            closes = [b.close for b in sorted_bars]
            mom = _price_momentum(closes, _MOMENTUM_LOOKBACK, _MOMENTUM_SKIP)
            result[symbol] = {
                "last": closes[-1] if closes else None,
                "momentum_12_1": mom,
                "bar_count": len(closes),
            }
        return result

    def _format_context(
        self,
        state: AgentState,
        factor_signals: dict[str, dict[str, Any]],
    ) -> str:
        def fmt(v: Any) -> str:
            return f"{float(v):.4f}" if v is not None else "n/a"

        ranked = sorted(
            [(sym, d) for sym, d in factor_signals.items() if d["momentum_12_1"] is not None],
            key=lambda x: x[1]["momentum_12_1"],
            reverse=True,
        )
        top25 = ranked[:25]
        candidate_rows = [
            f"  {rank + 1:2}. {sym:8} | last={fmt(d['last'])}"
            f" | 12-1_mom={fmt(d['momentum_12_1'])}"
            for rank, (sym, d) in enumerate(top25)
        ]

        positions_str = (
            ", ".join(f"{p.symbol}:{p.qty}" for p in state.positions)
            if state.positions
            else "flat"
        )
        regime = state.manager_regime_text or "(none this week)"
        critique = state.manager_critique or "(none)"
        recent = self._memory.recent_intents_summary(5)

        return "\n".join([
            f"=== SonnetAgent context @ {state.timestamp.isoformat()} ===",
            "",
            "Portfolio state:",
            f"  positions          : {positions_str}",
            f"  cash               : {float(state.account.cash):.2f}",
            f"  effective_max_gross: {float(state.effective_max_gross):.2f}",
            "",
            "Top factor-ranked candidates (12-1 momentum proxy):",
            *(candidate_rows or ["  (insufficient history)"]),
            "",
            f"Manager regime: {regime}",
            f"Manager critique: {critique}",
            "",
            f"Recent intents:\n{recent}",
            "",
            "Today's question: Review the factor rankings. Are any top-25 names "
            "ready to add, trim, or exit based on factor strength and news? "
            "Return JSON only.",
        ])

    def _parse_intents(self, response_text: str, state: AgentState) -> list[Intent]:
        data: dict[str, Any]
        try:
            data = json.loads(response_text)
        except json.JSONDecodeError:
            start = response_text.find("{")
            end = response_text.rfind("}")
            if start == -1 or end == -1 or end <= start:
                _log.warning("SonnetAgent: could not find JSON block in response")
                return []
            try:
                data = json.loads(response_text[start : end + 1])
            except json.JSONDecodeError:
                _log.warning("SonnetAgent: JSON extraction failed", exc_info=True)
                return []

        obs = str(data.get("market_observation", ""))
        intents: list[Intent] = []

        for item in data.get("intents", [])[: _MAX_INTENTS]:
            try:
                raw_action = str(item.get("action", "buy"))
                mapped = _ACTION_MAP.get(raw_action, raw_action)
                action = Action(mapped)
                intent = Intent(
                    id=new_id(),
                    agent_id=AgentId.SONNET,
                    symbol=str(item["symbol"]),
                    action=action,
                    target_weight=Decimal(str(item.get("target_weight", 0))),
                    sleeve=Sleeve.EQUITY,
                    signal=f"factor_rank={item.get('factor_score_rank', '?')}",
                    conviction=max(1, min(10, int(item.get("conviction", 5)))),
                    rationale=str(item.get("thesis", item.get("rationale", "")))[:280],
                    timestamp=state.timestamp,
                    regime_observation=obs[:200],
                )
                intents.append(intent)
            except (KeyError, ValueError):
                _log.warning("SonnetAgent: skipping malformed intent", exc_info=True)

        return intents
