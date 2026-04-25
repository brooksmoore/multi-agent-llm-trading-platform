"""Offline tests for SonnetAgent, OpusAgent, and ManagerAgent — all LLM calls mocked."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from agents.base import AgentState
from agents.llm import BudgetExhausted, LLMClient
from agents.manager_agent import ManagerAgent
from agents.memory import AgentMemory
from agents.opus_agent import OpusAgent
from agents.sonnet_agent import SonnetAgent
from core.types import (
    Action,
    AgentId,
    AgentMemo,
    Intent,
    KillSwitchState,
    Sleeve,
    new_id,
)
from data.market import Bar
from execution.broker import BrokerAccount

# ── Fixtures / helpers ─────────────────────────────────────────────────────────

_TS = datetime(2026, 4, 25, 17, 0, tzinfo=UTC)
_HAIKU = "claude-haiku-4-5-20251001"

_SONNET_RESPONSE = json.dumps({
    "market_observation": "Value + momentum composite looks constructive",
    "intents": [
        {
            "symbol": "NVDA",
            "action": "buy",
            "target_weight": 0.10,
            "factor_score_rank": 3,
            "thesis": "Top composite Z-score; momentum and quality both strong.",
            "risks": "AI capex cycle could reverse.",
            "conviction": 8,
            "expected_horizon_days": 30,
        }
    ],
    "calibration_note": "Last 5 calls: 3 wins, 1 loss, 1 flat.",
    "next_check": "tomorrow open",
})

_OPUS_DAILY_RESPONSE = json.dumps({
    "portfolio_observation": "TSM thesis intact; earnings in 14 days, size appropriate.",
    "intents": [
        {
            "symbol": "TSM",
            "action": "add",
            "target_weight": 0.15,
            "thesis_id": "TSM-2026-01",
            "trigger": "Strong Q1 CoWoS demand data validates thesis.",
            "conviction": 8,
            "expected_horizon_days": 90,
        }
    ],
    "thesis_health_check": [
        {"thesis_id": "TSM-2026-01", "status": "strengthening", "note": "Demand data firm."}
    ],
    "calibration_note": "Conviction 8 calls correct 70% of time historically.",
})

_OPUS_DEEP_DIVE_RESPONSE = json.dumps({
    "deep_dive_for": "TSM",
    "bull_case": "CoWoS capacity expansion drives pricing power through 2027.",
    "bear_case": "Taiwan geopolitical risk and customer concentration are real.",
    "delta_since_last": "AI demand guidance raised; TSMC now expects 30% AI revenue.",
    "conviction_prior": 7,
    "conviction_new": 8,
    "conviction_move_reason": "Demand visibility improved with Apple iPhone cycle clarity.",
    "kill_criteria": [
        "Taiwan Strait military incident",
        "Nvidia capex cut >20%",
        "Gross margin falls below 50%",
    ],
    "catalyst_calendar": [
        {"date": "2026-05-22", "event": "Q1 earnings", "watch_for": "AI revenue mix %"},
    ],
    "intent": {
        "action": "hold",
        "target_weight": 0.15,
        "rationale": "Conviction raised; no action needed this week.",
    },
})

_REGIME_RESPONSE = json.dumps({
    "regime_label": "risk_on",
    "vol_regime": "normal",
    "rate_regime": "easing",
    "macro_observation": "Fed dovish pivot supports risk assets broadly.",
    "key_events_ahead": [
        {"date": "2026-05-01", "event": "FOMC", "expected_impact": "Likely 25bps cut."}
    ],
    "agent_advice": {
        "haiku": "Trend signals reliable in current low-vol regime.",
        "sonnet": "Quality factor over value given rate-sensitivity.",
        "opus": "Tech concentration risk elevated; watch semis.",
    },
    "regime_change_from_last_week": False,
})

_CRITIQUE_RESPONSE = json.dumps({
    "critiques": [
        {
            "agent": "opus",
            "intent_id": "some-uuid",
            "summary_of_intent": "Buy TSM at 15% weight.",
            "red_team_objection": "Taiwan risk not fully priced; single-country concentration.",
            "what_evidence_would_change_my_mind": "Taiwan Strait tension index falls below 30.",
            "severity": "minor",
        }
    ]
})

_REALLOC_RESPONSE = json.dumps({
    "decision_basis": "Sonnet 4-week Sortino 1.42 leads haiku 0.87 and opus 1.01.",
    "current_allocation": {"haiku": 1000, "sonnet": 1000, "opus": 1000},
    "new_allocation": {"haiku": 950, "sonnet": 1100, "opus": 950},
    "max_step_respected": True,
    "winning_sleeve_4w_sortino": 1.42,
    "rationale": "Reward signal-to-noise; Sonnet's factor quality edge is repeatable.",
    "next_review_date": "2026-05-22",
})

_RISK_CHECK_RESPONSE = json.dumps({
    "intent_id": "some-uuid",
    "decision": "approve",
    "downsize_to_weight": None,
    "reason": "Weight 0.10 is within single-name cap; conviction 8 adequate.",
})

_DRAWDOWN_RESPONSE = json.dumps({
    "trigger": "halve_sizes",
    "drawdown_pct": -16.2,
    "peak_date": "2026-04-01",
    "trough_date": "2026-04-25",
    "attribution_by_sleeve": {"haiku": -0.04, "sonnet": -0.07, "opus": -0.05},
    "postmortem_required": True,
    "first_actions": [
        "Halve all open position sizes immediately.",
        "Suspend new entries for 48h.",
    ],
})

_MC_PROPOSAL_RESPONSE = json.dumps({
    "current_mc": 1.0,
    "proposed_mc": 1.10,
    "trigger": "human_review_required_to_raise",
    "evidence": {
        "weeks_since_last_change": 8,
        "rolling_sharpe_30d": 0.95,
        "agg_max_dd_30d": -0.048,
        "friction_bps_per_month": 22,
    },
    "rationale": "All raise conditions met; proposing incremental step to 1.10.",
    "requires_human_approval": True,
})


def _mock_memo(agent_id: AgentId = AgentId.HAIKU, call_type: str = "test") -> AgentMemo:
    return AgentMemo(
        id=new_id(),
        agent_id=agent_id,
        call_type=call_type,
        model=_HAIKU,
        timestamp=_TS,
        cached_tokens=0,
        new_input_tokens=500,
        output_tokens=100,
        cost_usd=Decimal("0.001"),
        prompt_hash="abc",
        response_json="{}",
        intents_emitted=0,
    )


def _make_account(cash: str = "1000.00") -> BrokerAccount:
    return BrokerAccount(
        cash=Decimal(cash),
        equity=Decimal("3000.00"),
        buying_power=Decimal("6000.00"),
        pattern_day_trader=False,
        daytrade_count=0,
    )


def _minimal_state(kill_switch: KillSwitchState = KillSwitchState.OK) -> AgentState:
    return AgentState(
        timestamp=_TS,
        bars_by_symbol={},
        news=[],
        positions=[],
        account=_make_account(),
        kill_switch_state=kill_switch,
        master_capability=Decimal("1.0"),
        effective_max_gross=Decimal("1.25"),
    )


def _make_bars(symbol: str, n: int, base_price: float = 100.0) -> list[Bar]:
    bars = []
    for i in range(n):
        ts = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=i)
        price = Decimal(str(round(base_price + i * 0.1, 2)))
        bars.append(
            Bar(
                symbol=symbol, timestamp=ts, open=price,
                high=price * Decimal("1.01"), low=price * Decimal("0.99"),
                close=price, volume=1_000_000,
            )
        )
    return bars


def _make_intent(
    symbol: str = "NVDA",
    action: Action = Action.BUY,
    conviction: int = 8,
    agent_id: AgentId = AgentId.OPUS,
) -> Intent:
    return Intent(
        id=new_id(),
        agent_id=agent_id,
        symbol=symbol,
        action=action,
        target_weight=Decimal("0.10"),
        sleeve=Sleeve.EQUITY,
        signal="test signal",
        conviction=conviction,
        rationale="Strong bull thesis with clear kill criteria.",
        timestamp=_TS,
    )


# ── SonnetAgent tests ─────────────────────────────────────────────────────────


def _make_sonnet() -> tuple[SonnetAgent, MagicMock, AgentMemory]:
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.return_value = (_SONNET_RESPONSE, _mock_memo(AgentId.SONNET))
    mem = AgentMemory(":memory:", AgentId.SONNET)
    agent = SonnetAgent(llm=mock_llm, memory=mem)
    return agent, mock_llm, mem


def test_sonnet_returns_intents_on_valid_response() -> None:
    agent, _, mem = _make_sonnet()
    intents = agent.observe(_minimal_state())
    assert len(intents) == 1
    assert intents[0].symbol == "NVDA"
    assert intents[0].conviction == 8
    assert intents[0].agent_id == AgentId.SONNET
    mem.close()


def test_sonnet_returns_empty_on_liquidate() -> None:
    agent, _, mem = _make_sonnet()
    intents = agent.observe(_minimal_state(KillSwitchState.DRAWDOWN_LIQUIDATE))
    assert intents == []
    mem.close()


def test_sonnet_returns_empty_on_bad_json() -> None:
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.return_value = ("not json", _mock_memo())
    mem = AgentMemory(":memory:", AgentId.SONNET)
    agent = SonnetAgent(llm=mock_llm, memory=mem)
    assert agent.observe(_minimal_state()) == []
    mem.close()


def test_sonnet_returns_empty_on_budget_exhausted() -> None:
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.side_effect = BudgetExhausted("over budget")
    mem = AgentMemory(":memory:", AgentId.SONNET)
    agent = SonnetAgent(llm=mock_llm, memory=mem)
    assert agent.observe(_minimal_state()) == []
    mem.close()


def test_sonnet_caps_intents_at_five() -> None:
    big_response = json.dumps({
        "market_observation": "many signals",
        "intents": [
            {
                "symbol": f"SYM{i}", "action": "buy", "target_weight": 0.05,
                "factor_score_rank": i + 1,
                "thesis": "strong", "risks": "risk", "conviction": 6,
                "expected_horizon_days": 30,
            }
            for i in range(8)
        ],
        "calibration_note": "",
        "next_check": "tomorrow",
    })
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.return_value = (big_response, _mock_memo())
    mem = AgentMemory(":memory:", AgentId.SONNET)
    agent = SonnetAgent(llm=mock_llm, memory=mem)
    intents = agent.observe(_minimal_state())
    assert len(intents) <= 5
    mem.close()


def test_sonnet_maps_trim_to_sell() -> None:
    response = json.dumps({
        "market_observation": "trimming overweight",
        "intents": [
            {
                "symbol": "AAPL", "action": "trim", "target_weight": 0.05,
                "factor_score_rank": 10, "thesis": "overweight vs rank",
                "risks": "momentum could continue", "conviction": 5,
                "expected_horizon_days": 10,
            }
        ],
        "calibration_note": "",
        "next_check": "1w",
    })
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.return_value = (response, _mock_memo())
    mem = AgentMemory(":memory:", AgentId.SONNET)
    agent = SonnetAgent(llm=mock_llm, memory=mem)
    intents = agent.observe(_minimal_state())
    assert len(intents) == 1
    assert intents[0].action == Action.SELL
    mem.close()


def test_sonnet_records_intents_in_memory() -> None:
    agent, _, mem = _make_sonnet()
    intents = agent.observe(_minimal_state())
    assert len(intents) == 1
    summary = mem.recent_intents_summary(3)
    assert "NVDA" in summary
    mem.close()


def test_sonnet_compute_factor_signals_with_bars() -> None:
    agent, _, mem = _make_sonnet()
    bars = {"NVDA": _make_bars("NVDA", 280)}
    state = _minimal_state()
    state.bars_by_symbol.update(bars)
    signals = agent._compute_factor_signals(bars)
    assert "NVDA" in signals
    assert signals["NVDA"]["bar_count"] == 280
    mem.close()


# ── OpusAgent tests ───────────────────────────────────────────────────────────


def _make_opus() -> tuple[OpusAgent, MagicMock, AgentMemory]:
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.return_value = (_OPUS_DAILY_RESPONSE, _mock_memo(AgentId.OPUS))
    mem = AgentMemory(":memory:", AgentId.OPUS)
    agent = OpusAgent(llm=mock_llm, memory=mem)
    return agent, mock_llm, mem


def test_opus_returns_intents_on_valid_response() -> None:
    agent, _, mem = _make_opus()
    intents = agent.observe(_minimal_state())
    assert len(intents) == 1
    assert intents[0].symbol == "TSM"
    assert intents[0].action == Action.BUY   # "add" → "buy"
    assert intents[0].conviction == 8
    mem.close()


def test_opus_returns_empty_on_liquidate() -> None:
    agent, _, mem = _make_opus()
    intents = agent.observe(_minimal_state(KillSwitchState.DRAWDOWN_LIQUIDATE))
    assert intents == []
    mem.close()


def test_opus_returns_empty_on_bad_json() -> None:
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.return_value = ("totally broken", _mock_memo())
    mem = AgentMemory(":memory:", AgentId.OPUS)
    agent = OpusAgent(llm=mock_llm, memory=mem)
    assert agent.observe(_minimal_state()) == []
    mem.close()


def test_opus_returns_empty_on_budget_exhausted() -> None:
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.side_effect = BudgetExhausted("over budget")
    mem = AgentMemory(":memory:", AgentId.OPUS)
    agent = OpusAgent(llm=mock_llm, memory=mem)
    assert agent.observe(_minimal_state()) == []
    mem.close()


def test_opus_caps_intents_at_three() -> None:
    response = json.dumps({
        "portfolio_observation": "many intents",
        "intents": [
            {
                "symbol": f"SYM{i}", "action": "buy", "target_weight": 0.10,
                "thesis_id": f"SYM{i}-2026", "trigger": "strong thesis",
                "conviction": 7, "expected_horizon_days": 60,
            }
            for i in range(5)
        ],
        "thesis_health_check": [],
        "calibration_note": "",
    })
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.return_value = (response, _mock_memo())
    mem = AgentMemory(":memory:", AgentId.OPUS)
    agent = OpusAgent(llm=mock_llm, memory=mem)
    intents = agent.observe(_minimal_state())
    assert len(intents) <= 3
    mem.close()


def test_opus_skips_hold_action() -> None:
    response = json.dumps({
        "portfolio_observation": "quiet day",
        "intents": [
            {
                "symbol": "TSM", "action": "hold", "target_weight": 0.15,
                "thesis_id": "TSM-2026-01", "trigger": "no change",
                "conviction": 7, "expected_horizon_days": 90,
            }
        ],
        "thesis_health_check": [],
        "calibration_note": "",
    })
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.return_value = (response, _mock_memo())
    mem = AgentMemory(":memory:", AgentId.OPUS)
    agent = OpusAgent(llm=mock_llm, memory=mem)
    intents = agent.observe(_minimal_state())
    assert intents == []
    mem.close()


def test_opus_deep_dive_returns_dict() -> None:
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.return_value = (_OPUS_DEEP_DIVE_RESPONSE, _mock_memo(AgentId.OPUS, "deep_dive"))
    mem = AgentMemory(":memory:", AgentId.OPUS)
    agent = OpusAgent(llm=mock_llm, memory=mem)
    result = agent.deep_dive(_minimal_state(), "TSM", "Document pack placeholder.")
    assert result.get("deep_dive_for") == "TSM"
    assert "bull_case" in result
    assert "bear_case" in result
    assert "kill_criteria" in result
    mem.close()


def test_opus_deep_dive_returns_empty_on_llm_failure() -> None:
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.side_effect = BudgetExhausted("over budget")
    mem = AgentMemory(":memory:", AgentId.OPUS)
    agent = OpusAgent(llm=mock_llm, memory=mem)
    result = agent.deep_dive(_minimal_state(), "TSM", "doc pack")
    assert result == {}
    mem.close()


def test_opus_records_intents_in_memory() -> None:
    agent, _, mem = _make_opus()
    intents = agent.observe(_minimal_state())
    assert len(intents) == 1
    summary = mem.recent_intents_summary(3)
    assert "TSM" in summary
    mem.close()


# ── ManagerAgent tests ────────────────────────────────────────────────────────


def _make_manager(response: str) -> tuple[ManagerAgent, MagicMock, AgentMemory]:
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.return_value = (response, _mock_memo(AgentId.MANAGER))
    mem = AgentMemory(":memory:", AgentId.MANAGER)
    agent = ManagerAgent(llm=mock_llm, memory=mem)
    return agent, mock_llm, mem


def test_manager_regime_read_returns_dict() -> None:
    agent, _, mem = _make_manager(_REGIME_RESPONSE)
    result = agent.regime_read(_minimal_state(), prior_regime="risk_on")
    assert result.get("regime_label") == "risk_on"
    assert "agent_advice" in result
    assert "macro_observation" in result
    mem.close()


def test_manager_regime_read_returns_empty_on_llm_failure() -> None:
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.side_effect = BudgetExhausted("over budget")
    mem = AgentMemory(":memory:", AgentId.MANAGER)
    agent = ManagerAgent(llm=mock_llm, memory=mem)
    result = agent.regime_read(_minimal_state())
    assert result == {}
    mem.close()


def test_manager_adversarial_critique_returns_dict() -> None:
    agent, _, mem = _make_manager(_CRITIQUE_RESPONSE)
    intents = [_make_intent()]
    result = agent.adversarial_critique(_minimal_state(), intents)
    assert "critiques" in result
    assert len(result["critiques"]) == 1
    assert result["critiques"][0]["severity"] == "minor"
    mem.close()


def test_manager_capital_reallocation_returns_dict() -> None:
    agent, _, mem = _make_manager(_REALLOC_RESPONSE)
    snapshot = "haiku: Sortino=0.87, sonnet: Sortino=1.42, opus: Sortino=1.01"
    result = agent.capital_reallocation(_minimal_state(), snapshot)
    assert "new_allocation" in result
    assert result["max_step_respected"] is True
    mem.close()


def test_manager_risk_check_approve() -> None:
    agent, _, mem = _make_manager(_RISK_CHECK_RESPONSE)
    intent = _make_intent()
    result = agent.risk_check(_minimal_state(), intent)
    assert result.get("decision") == "approve"
    mem.close()


def test_manager_drawdown_response_returns_dict() -> None:
    agent, _, mem = _make_manager(_DRAWDOWN_RESPONSE)
    result = agent.drawdown_response(
        _minimal_state(),
        drawdown_pct=-0.162,
        attribution={"haiku": -0.04, "sonnet": -0.07, "opus": -0.05},
    )
    assert result.get("trigger") == "halve_sizes"
    assert result["postmortem_required"] is True
    mem.close()


def test_manager_weekly_journal_returns_string() -> None:
    journal_text = "# Week 01\n\nSolid week.\n"
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.return_value = (journal_text, _mock_memo(AgentId.MANAGER, "weekly_journal"))
    mem = AgentMemory(":memory:", AgentId.MANAGER)
    agent = ManagerAgent(llm=mock_llm, memory=mem)
    result = agent.weekly_journal(_minimal_state(), week_data="Portfolio data here.")
    assert "Week" in result
    mem.close()


def test_manager_weekly_journal_returns_empty_on_failure() -> None:
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.side_effect = RuntimeError("API down")
    mem = AgentMemory(":memory:", AgentId.MANAGER)
    agent = ManagerAgent(llm=mock_llm, memory=mem)
    result = agent.weekly_journal(_minimal_state(), week_data="data")
    assert result == ""
    mem.close()


def test_manager_master_capability_proposal_returns_dict() -> None:
    agent, _, mem = _make_manager(_MC_PROPOSAL_RESPONSE)
    evidence = {
        "weeks_since_last_change": 8,
        "rolling_sharpe_30d": 0.95,
        "agg_max_dd_30d": -0.048,
        "friction_bps_per_month": 22,
    }
    result = agent.master_capability_proposal(_minimal_state(), evidence)
    assert result.get("proposed_mc") == pytest.approx(1.10)
    assert result.get("requires_human_approval") is True
    mem.close()


def test_manager_bad_json_returns_empty_dict() -> None:
    agent, _, mem = _make_manager("not json at all")
    result = agent.regime_read(_minimal_state())
    assert result == {}
    mem.close()
