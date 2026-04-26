"""HaikuAgent: Faber GTAA equity trend + crypto SMA/momentum dual-mandate."""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

from agents.base import AgentState, BaseAgent
from agents.json_utils import parse_json_object
from agents.llm import LLMClient
from agents.memory import AgentMemory
from core.types import Action, AgentId, Intent, KillSwitchState, Sleeve, new_id
from data.market import Bar

_log = logging.getLogger(__name__)

_EQUITY_UNIVERSE = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "USO", "VNQ"]
_CRYPTO_UNIVERSE = ["BTCUSD", "ETHUSD", "SOLUSD"]
_SMA_PERIOD_EQUITY = 210
_SMA_PERIOD_CRYPTO = 50
_MOMENTUM_DAYS = 14
_MAX_INTENTS = 4
_PROMPT_PATH = Path(__file__).parent / "prompts" / "haiku_agent.md"


def _sma(closes: list[Decimal], period: int) -> Decimal | None:
    """Simple moving average; returns None if insufficient history."""
    if len(closes) < period:
        return None
    window = closes[-period:]
    return sum(window, Decimal("0")) / Decimal(str(period))


def _momentum(closes: list[Decimal], days: int) -> Decimal | None:
    """Returns (close[-1]/close[-(days+1)]) - 1; None if insufficient history."""
    if len(closes) < days + 1 or closes[-(days + 1)] == Decimal("0"):
        return None
    return closes[-1] / closes[-(days + 1)] - Decimal("1")


class HaikuAgent(BaseAgent):
    def __init__(self, llm: LLMClient, memory: AgentMemory) -> None:
        super().__init__(AgentId.HAIKU)
        self._llm = llm
        self._memory = memory
        self._prompt = _PROMPT_PATH.read_text()

    def observe(self, state: AgentState) -> list[Intent]:
        """Process system state snapshot, return zero or more trade intents."""
        if state.kill_switch_state == KillSwitchState.DRAWDOWN_LIQUIDATE:
            _log.warning("kill switch DRAWDOWN_LIQUIDATE — haiku skipping cycle")
            return []

        equity_trend = self._compute_equity_trend(state.bars_by_symbol)
        crypto_trend = self._compute_crypto_trend(state.bars_by_symbol)
        context = self._format_context(state, equity_trend, crypto_trend)

        try:
            response_text, memo = self._llm.call(
                system=self._prompt,
                user=context,
                agent_id=AgentId.HAIKU,
                call_type="trend_observe",
                max_tokens=1024,
            )
        except Exception:
            _log.warning("LLM call failed in HaikuAgent.observe", exc_info=True)
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

    def _compute_equity_trend(
        self, bars_by_symbol: dict[str, list[Bar]]
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for symbol in _EQUITY_UNIVERSE:
            bars = sorted(bars_by_symbol.get(symbol, []), key=lambda b: b.timestamp)
            closes = [b.close for b in bars]
            last = closes[-1] if closes else None
            sma = _sma(closes, _SMA_PERIOD_EQUITY)
            in_trend = last is not None and sma is not None and last > sma
            result[symbol] = {"last": last, "sma": sma, "in_trend": in_trend}
        return result

    def _compute_crypto_trend(
        self, bars_by_symbol: dict[str, list[Bar]]
    ) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for symbol in _CRYPTO_UNIVERSE:
            bars = sorted(bars_by_symbol.get(symbol, []), key=lambda b: b.timestamp)
            closes = [b.close for b in bars]
            last = closes[-1] if closes else None
            sma = _sma(closes, _SMA_PERIOD_CRYPTO)
            mom = _momentum(closes, _MOMENTUM_DAYS)
            in_trend = (
                last is not None
                and sma is not None
                and last > sma
                and mom is not None
                and mom > Decimal("0")
            )
            result[symbol] = {
                "last": last,
                "sma": sma,
                "momentum_14d": mom,
                "in_trend": in_trend,
            }
        return result

    def _format_context(
        self,
        state: AgentState,
        equity_trend: dict[str, dict[str, Any]],
        crypto_trend: dict[str, dict[str, Any]],
    ) -> str:
        def fmt_price(v: Any) -> str:
            return f"{float(v):.2f}" if v is not None else "n/a"

        equity_syms = set(_EQUITY_UNIVERSE)
        crypto_syms = set(_CRYPTO_UNIVERSE)

        eq_positions = [p for p in state.positions if p.symbol in equity_syms]
        cr_positions = [p for p in state.positions if p.symbol in crypto_syms]

        if eq_positions:
            equity_summary = ", ".join(f"{p.symbol}:{p.qty}" for p in eq_positions)
        else:
            equity_summary = "flat"

        if cr_positions:
            crypto_summary = ", ".join(f"{p.symbol}:{p.qty}" for p in cr_positions)
        else:
            crypto_summary = "flat"

        etf_rows = []
        for sym in _EQUITY_UNIVERSE:
            d = equity_trend[sym]
            trend_label = "IN" if d["in_trend"] else "OUT"
            etf_rows.append(
                f"  {sym:6} | last={fmt_price(d['last'])} | 10mo_sma={fmt_price(d['sma'])}"
                f" | {trend_label}"
            )

        crypto_rows = []
        for sym in _CRYPTO_UNIVERSE:
            d = crypto_trend[sym]
            mom = d["momentum_14d"]
            mom_str = f"{float(mom) * 100:.1f}%" if mom is not None else "n/a%"
            trend_label = "IN" if d["in_trend"] else "OUT"
            crypto_rows.append(
                f"  {sym:8} | last={fmt_price(d['last'])} | 50d_sma={fmt_price(d['sma'])}"
                f" | 14d_mom={mom_str} | {trend_label}"
            )

        regime = state.manager_regime_text or "(none this week)"
        recent = self._memory.recent_intents_summary(3)
        critique = state.manager_critique or "(none)"

        return "\n".join([
            f"=== HaikuAgent context @ {state.timestamp.isoformat()} ===",
            "",
            "Portfolio state:",
            f"  equity positions : {equity_summary}",
            f"  crypto positions : {crypto_summary}",
            f"  cash             : {fmt_price(state.account.cash)}",
            f"  effective_max_gross: {fmt_price(state.effective_max_gross)}",
            "",
            "ETF trend table (Faber 10-month SMA):",
            *etf_rows,
            "",
            "Crypto trend table (50d SMA + 14d momentum):",
            *crypto_rows,
            "",
            f"Manager regime: {regime}",
            "",
            f"Recent intents:\n{recent}",
            "",
            f"Manager critique: {critique}",
            "",
            "Today's question: What trend signals are flipping today? "
            "Which ETFs just crossed above/below their 10-month SMA? "
            "Which crypto assets are confirming or losing trend? "
            "Respond with JSON only.",
        ])

    def _parse_intents(self, response_text: str, state: AgentState) -> list[Intent]:
        data = parse_json_object(response_text)
        if data is None:
            _log.warning("HaikuAgent: could not parse JSON from LLM response")
            return []

        regime = str(data.get("regime_observation", ""))
        intents: list[Intent] = []

        for item in data.get("intents", [])[: _MAX_INTENTS]:
            try:
                action = Action(item["action"])
                try:
                    sleeve = Sleeve(item.get("sleeve", "equity"))
                except ValueError:
                    sleeve = Sleeve.EQUITY
                intent = Intent(
                    id=new_id(),
                    agent_id=AgentId.HAIKU,
                    symbol=str(item["symbol"]),
                    action=action,
                    target_weight=Decimal(str(item.get("target_weight", 0))),
                    sleeve=sleeve,
                    signal=str(item.get("signal", ""))[:140],
                    conviction=max(1, min(10, int(item.get("conviction", 5)))),
                    rationale=str(item.get("rationale", ""))[:280],
                    timestamp=state.timestamp,
                    regime_observation=regime[:200],
                )
                intents.append(intent)
            except (KeyError, ValueError):
                _log.warning("HaikuAgent: skipping malformed intent item", exc_info=True)

        return intents
