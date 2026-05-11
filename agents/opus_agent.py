"""OpusAgent: Concentrated GARP discretionary PM with scheduled deep-dives."""

from __future__ import annotations

import hashlib
import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

from agents.base import AgentState, BaseAgent, format_news_block, render_system_prompt
from agents.llm import LLMClient
from agents.memory import AgentMemory
from core.types import Action, AgentId, Intent, KillSwitchState, Sleeve, new_id

_log = logging.getLogger(__name__)

_MAX_INTENTS = 3
_PROMPT_PATH = Path(__file__).parent / "prompts" / "opus_agent.md"

# Target book size for "concentrated 5–8 names" mandate. Fewer holdings ⇒
# the daily prompt switches into initiation mode (seed candidates) rather
# than management mode (review existing book).
TARGET_HOLDINGS: int = 5

# Cap on starter sizes coming out of initiation-mode daily intents. Real
# conviction sizing comes after the deep-dive earns it.
_INITIATION_MAX_TARGET_WEIGHT: Decimal = Decimal("0.05")

# Persisted in opus memory under this key — comma-separated, max ~20 names.
_WATCHLIST_KEY: str = "opus:watchlist"
_WATCHLIST_MAX: int = 20

# "hold" maps to None (no order emitted); others map to core Action strings
_ACTION_MAP: dict[str, str | None] = {
    "trim": "sell",
    "add": "buy",
    "buy": "buy",
    "rebalance_to": "rebalance_to",
    "exit": "sell",
    "sell": "sell",
    "hold": None,
}


class OpusAgent(BaseAgent):
    def __init__(self, llm: LLMClient, memory: AgentMemory) -> None:
        super().__init__(AgentId.OPUS)
        self._llm = llm
        self._memory = memory
        self._prompt = _PROMPT_PATH.read_text()

    def observe(self, state: AgentState) -> list[Intent]:
        """Daily thesis health check; return ≤3 intents.

        Switches between initiation mode (book under-built — propose starter
        intents and watchlist additions) and management mode (review existing
        book) based on holdings count.
        """
        if state.kill_switch_state == KillSwitchState.DRAWDOWN_LIQUIDATE:
            _log.warning("kill switch DRAWDOWN_LIQUIDATE — opus skipping cycle")
            return []

        mode = "initiation" if len(state.positions) < TARGET_HOLDINGS else "management"
        context = self._format_daily_context(state, mode=mode)

        try:
            response_text, _ = self._llm.call(
                system=render_system_prompt(self._prompt, state),
                user=context,
                agent_id=AgentId.OPUS,
                call_type="daily_check",
                max_tokens=2048,
            )
        except Exception:
            _log.warning("LLM call failed in OpusAgent.observe", exc_info=True)
            return []

        intents = self._parse_daily_intents(response_text, state, mode=mode)
        self._merge_watchlist_from_response(response_text)

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

    def signal_fingerprint(self, state: AgentState) -> str | None:
        """Return a deterministic hash of Opus's signal inputs, or None to disable.

        Returns None during initiation mode (holdings under target_count) so
        that book-building cycles always run — initiation needs to keep
        emitting starter intents and watchlist adds even when no other input
        has changed.

        Otherwise hashes:
          - sorted (symbol, qty) pairs from the sleeve's holdings
          - sorted watchlist symbols
          - effective_max_gross (cap changes invalidate the cycle)
          - manager_directive (any new Manager guidance invalidates it)

        Quantized to int qty + 4-decimal EMG so trivial mark-to-market noise
        and Decimal precision artifacts do not invalidate the fingerprint.
        """
        if len(state.positions) < TARGET_HOLDINGS:
            return None

        # Quantize qty to 2 decimals (handles fractional shares from paper
        # trading; blocks Decimal-arithmetic noise from invalidating the fp).
        holdings = sorted(
            (p.symbol.upper(), format(p.qty.quantize(Decimal("0.01")), "f"))
            for p in state.positions
        )
        watchlist = sorted(self.get_watchlist())
        emg_q = format(state.effective_max_gross.quantize(Decimal("0.0001")), "f")
        directive = (state.manager_directive or "").strip()

        payload = json.dumps(
            {"h": holdings, "w": watchlist, "emg": emg_q, "md": directive},
            sort_keys=True, separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    # ── Watchlist ─────────────────────────────────────────────────────────────

    def get_watchlist(self) -> list[str]:
        """Return the persisted watchlist (most-recent additions first)."""
        raw = self._memory.recall(_WATCHLIST_KEY) or ""
        return [s for s in (sym.strip().upper() for sym in raw.split(",")) if s]

    def _merge_watchlist_from_response(self, response_text: str) -> None:
        data = self._parse_json(response_text, context="watchlist_add")
        adds = data.get("watchlist_add") or []
        if not isinstance(adds, list) or not adds:
            return
        held = {p.symbol.upper() for p in []}  # placeholder; held check happens in app.py
        cleaned: list[str] = []
        for item in adds:
            sym = str(item).strip().upper()
            if sym and sym not in held:
                cleaned.append(sym)
        if not cleaned:
            return
        existing = self.get_watchlist()
        # New entries first; keep order, dedupe, cap.
        merged: list[str] = []
        for sym in [*cleaned, *existing]:
            if sym not in merged:
                merged.append(sym)
        merged = merged[:_WATCHLIST_MAX]
        self._memory.remember(_WATCHLIST_KEY, ",".join(merged))

    def deep_dive(self, state: AgentState, symbol: str, doc_pack: str) -> dict[str, Any]:
        """Run a full Thursday/Friday deep-dive for one holding or watchlist name.

        Returns the parsed analysis dict. Use `extract_deep_dive_intent(...)`
        to derive an executable Intent from the dict's `intent` field.
        """
        held = symbol.upper() in {p.symbol.upper() for p in state.positions}
        flavor = "current holding" if held else "watchlist candidate (initiation)"
        user_msg = "\n".join([
            f"DEEP DIVE TARGET: {symbol} ({flavor})",
            f"Current portfolio equity: {float(state.account.equity):.2f}",
            "",
            doc_pack,
        ])

        try:
            response_text, _ = self._llm.call(
                system=render_system_prompt(self._prompt, state),
                user=user_msg,
                agent_id=AgentId.OPUS,
                call_type="deep_dive",
                max_tokens=4096,
            )
        except Exception:
            _log.warning("OpusAgent.deep_dive failed for %s", symbol, exc_info=True)
            return {}

        return self._parse_json(response_text, context=f"deep_dive({symbol})")

    def extract_deep_dive_intent(
        self, state: AgentState, data: dict[str, Any], symbol: str
    ) -> Intent | None:
        """Parse the `intent` field from a deep-dive analysis into an Intent.

        Returns None for `hold`, missing fields, or malformed actions.
        Conviction is sourced from `conviction_new` (deep-dives revise it).
        """
        item = data.get("intent")
        if not isinstance(item, dict):
            return None

        raw_action = str(item.get("action", "hold"))
        mapped = _ACTION_MAP.get(raw_action)
        if mapped is None:
            return None  # hold

        try:
            action = Action(mapped)
            conviction = max(1, min(10, int(data.get("conviction_new", 7))))
            target_weight = Decimal(str(item.get("target_weight", 0)))
            rationale = str(item.get("rationale", ""))[:280]
            return Intent(
                id=new_id(),
                agent_id=AgentId.OPUS,
                symbol=symbol,
                action=action,
                target_weight=target_weight,
                sleeve=Sleeve.EQUITY,
                signal=f"deep_dive:{symbol}"[:140],
                conviction=conviction,
                rationale=rationale,
                timestamp=state.timestamp,
                regime_observation=str(data.get("delta_since_last", ""))[:200],
            )
        except (KeyError, ValueError):
            _log.warning("OpusAgent: malformed deep-dive intent", exc_info=True)
            return None

    # ── Private helpers ────────────────────────────────────────────────────────

    def _format_daily_context(self, state: AgentState, *, mode: str = "management") -> str:
        positions_str = (
            ", ".join(f"{p.symbol}:{p.qty}" for p in state.positions)
            if state.positions
            else "flat"
        )
        watchlist = self.get_watchlist()
        watchlist_str = ", ".join(watchlist) if watchlist else "(empty)"
        regime = state.manager_regime_text or "(none this week)"
        critique = state.manager_critique or "(none)"
        morning_brief = state.manager_morning_brief or "(no brief today)"
        directive = state.manager_directive or "(no active directive)"
        recent = self._memory.recent_intents_summary(3)

        if mode == "initiation":
            question = (
                "Today's question (INITIATION MODE): your sleeve has fewer than "
                f"{TARGET_HOLDINGS} holdings. Either propose ≤2 starter intents "
                f"(target_weight ≤ {float(_INITIATION_MAX_TARGET_WEIGHT):.2f}, "
                "conviction ≥ 7), or use watchlist_add to queue candidates for "
                "this week's deep-dives — or both. Return JSON only."
            )
        else:
            question = (
                "Today's question: Review all active theses. Has anything "
                "broken today? Any catalyst in the next 5 days requiring "
                "position adjustment? Return JSON only."
            )

        return "\n".join([
            f"=== OpusAgent daily check @ {state.timestamp.isoformat()} ===",
            "",
            f"MODE              : {mode}",
            f"Holdings count    : {len(state.positions)} / target {TARGET_HOLDINGS}",
            f"Holdings          : {positions_str}",
            f"Watchlist         : {watchlist_str}",
            f"Cash              : {float(state.account.cash):.2f}",
            f"Effective max gross: {float(state.effective_max_gross):.2f}",
            "",
            f"Manager regime: {regime}",
            f"Manager morning brief (today): {morning_brief}",
            f"Manager directive (active): {directive}",
            f"Manager critique: {critique}",
            "",
            f"Recent intents:\n{recent}",
            "",
            format_news_block(state, limit=20),
            "",
            question,
        ])

    def _parse_daily_intents(
        self, response_text: str, state: AgentState, *, mode: str = "management"
    ) -> list[Intent]:
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
                target_weight = Decimal(str(item.get("target_weight", 0)))
                conviction = max(1, min(10, int(item.get("conviction", 5))))

                # Initiation-mode guardrails: starter intents must be small AND
                # high-conviction. The model is told this in the prompt; we
                # enforce it here so prompt drift can't blow up sizing.
                if mode == "initiation" and action == Action.BUY:
                    if conviction < 7:
                        _log.info(
                            "opus initiation: skip %s (conviction=%d < 7)",
                            item.get("symbol"), conviction,
                        )
                        continue
                    if target_weight > _INITIATION_MAX_TARGET_WEIGHT:
                        target_weight = _INITIATION_MAX_TARGET_WEIGHT

                intent = Intent(
                    id=new_id(),
                    agent_id=AgentId.OPUS,
                    symbol=str(item["symbol"]),
                    action=action,
                    target_weight=target_weight,
                    sleeve=Sleeve.EQUITY,
                    signal=str(item.get("trigger", item.get("thesis_id", "")))[:140],
                    conviction=conviction,
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
