"""Offline tests for agents/ — all LLM calls mocked, no real API access."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agents.base import AgentState
from agents.calibration import CalibrationTracker
from agents.haiku_agent import HaikuAgent, _momentum, _sma
from agents.llm import BudgetExhausted, LLMClient
from agents.memory import AgentMemory
from core.types import (
    AgentId,
    AgentMemo,
    KillSwitchState,
    new_id,
)
from data.market import Bar
from execution.broker import BrokerAccount
from execution.budget import BudgetLedger

# ── Fixtures / helpers ─────────────────────────────────────────────────────────

_TS = datetime(2026, 4, 25, 15, 0, tzinfo=UTC)
_HAIKU = "claude-haiku-4-5-20251001"

_VALID_RESPONSE = json.dumps({
    "regime_observation": "SPY in uptrend, all signals green",
    "intents": [
        {
            "symbol": "SPY",
            "action": "buy",
            "target_weight": 0.18,
            "sleeve": "equity",
            "signal": "SPY closed above 10mo SMA after 2 weeks below",
            "conviction": 7,
            "rationale": "Faber GTAA signal confirmed; momentum positive.",
        }
    ],
    "next_check": "next daily close",
})


def _make_budget(tmp_path: Path, limit: Decimal = Decimal("1.00")) -> BudgetLedger:
    return BudgetLedger(tmp_path / "budget.json", daily_limit=limit)


def _make_bars(symbol: str, n: int, base_price: float = 100.0) -> list[Bar]:
    bars = []
    for i in range(n):
        ts = datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=i)
        price = Decimal(str(round(base_price + i * 0.1, 2)))
        bars.append(
            Bar(symbol=symbol, timestamp=ts, open=price,
                high=price * Decimal("1.01"), low=price * Decimal("0.99"),
                close=price, volume=1_000_000)
        )
    return bars


def _make_account(cash: str = "1000.00") -> BrokerAccount:
    return BrokerAccount(
        cash=Decimal(cash), equity=Decimal("1000.00"),
        buying_power=Decimal("2000.00"), pattern_day_trader=False, daytrade_count=0,
    )


def _minimal_state(
    bars_by_symbol: dict[str, list[Bar]] | None = None,
    kill_switch: KillSwitchState = KillSwitchState.OK,
) -> AgentState:
    return AgentState(
        timestamp=_TS,
        bars_by_symbol=bars_by_symbol or {},
        news=[],
        positions=[],
        account=_make_account(),
        kill_switch_state=kill_switch,
        master_capability=Decimal("1.0"),
        effective_max_gross=Decimal("1.5"),
    )


def _mock_memo() -> AgentMemo:
    return AgentMemo(
        id=new_id(), agent_id=AgentId.HAIKU, call_type="trend_observe",
        model=_HAIKU, timestamp=_TS, cached_tokens=0,
        new_input_tokens=500, output_tokens=100, cost_usd=Decimal("0.001"),
        prompt_hash="abc", response_json=_VALID_RESPONSE, intents_emitted=1,
    )


# ── LLMClient tests ────────────────────────────────────────────────────────────


def test_llm_budget_exhausted_raises(tmp_path: Path) -> None:
    budget = _make_budget(tmp_path, limit=Decimal("0.00"))
    client = LLMClient(budget=budget, model=_HAIKU)
    with pytest.raises(BudgetExhausted):
        client.call("system", "user", AgentId.HAIKU, "test")


def test_llm_records_spend_after_call(tmp_path: Path) -> None:
    budget = _make_budget(tmp_path)
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text=_VALID_RESPONSE)]
    mock_response.usage = MagicMock(
        input_tokens=500, output_tokens=100,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    with patch("agents.llm.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        client = LLMClient(budget=budget, model=_HAIKU, api_key="test-key")
        text, memo = client.call("sys", "user", AgentId.HAIKU, "test")

    assert text == _VALID_RESPONSE
    assert budget.today_spent() > Decimal("0")
    assert memo.agent_id == AgentId.HAIKU
    assert memo.call_type == "test"


def test_llm_budget_exhausted_when_remaining_is_tiny(tmp_path: Path) -> None:
    budget = _make_budget(tmp_path, limit=Decimal("0.000001"))
    client = LLMClient(budget=budget, model=_HAIKU)
    with pytest.raises(BudgetExhausted):
        client.call("A" * 10000, "B" * 10000, AgentId.HAIKU, "test")


def test_llm_memo_has_correct_model(tmp_path: Path) -> None:
    budget = _make_budget(tmp_path)
    mock_response = MagicMock()
    mock_response.content = [MagicMock(text="{}")]
    mock_response.usage = MagicMock(
        input_tokens=10, output_tokens=5,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    with patch("agents.llm.anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = mock_response
        client = LLMClient(budget=budget, model=_HAIKU, api_key="key")
        _, memo = client.call("sys", "user", AgentId.HAIKU, "morning_brief")

    assert memo.model == _HAIKU


# ── AgentMemory tests ──────────────────────────────────────────────────────────


def test_memory_remember_and_recall() -> None:
    mem = AgentMemory(":memory:", AgentId.HAIKU)
    mem.remember("last_regime", "risk-on")
    assert mem.recall("last_regime") == "risk-on"
    mem.close()


def test_memory_recall_missing_returns_none() -> None:
    mem = AgentMemory(":memory:", AgentId.HAIKU)
    assert mem.recall("nonexistent") is None
    mem.close()


def test_memory_remember_overwrites() -> None:
    mem = AgentMemory(":memory:", AgentId.HAIKU)
    mem.remember("k", "v1")
    mem.remember("k", "v2")
    assert mem.recall("k") == "v2"
    mem.close()


def test_memory_journal_write_and_read() -> None:
    mem = AgentMemory(":memory:", AgentId.HAIKU)
    today = date(2026, 4, 25)
    mem.write_journal(today, "SPY above SMA; holding.")
    assert mem.read_journal(today) == "SPY above SMA; holding."
    mem.close()


def test_memory_journal_missing_returns_none() -> None:
    mem = AgentMemory(":memory:", AgentId.HAIKU)
    assert mem.read_journal(date(2020, 1, 1)) is None
    mem.close()


def test_memory_record_intent_and_summary() -> None:
    mem = AgentMemory(":memory:", AgentId.HAIKU)
    iid = new_id()
    mem.record_intent(iid, "SPY", "buy", 7, "Trend confirmed.", _TS)
    summary = mem.recent_intents_summary(3)
    assert "SPY" in summary
    assert "buy" in summary.lower()
    mem.close()


def test_memory_record_outcome_updates_intent() -> None:
    mem = AgentMemory(":memory:", AgentId.HAIKU)
    iid = new_id()
    mem.record_intent(iid, "QQQ", "sell", 6, "SMA cross below.", _TS)
    mem.record_outcome(iid, "win")
    summary = mem.recent_intents_summary(3)
    assert "win" in summary
    mem.close()


# ── CalibrationTracker tests ───────────────────────────────────────────────────


def test_calibration_brier_score_no_records() -> None:
    cal = CalibrationTracker(":memory:")
    assert cal.brier_score() == pytest.approx(0.0)
    cal.close()


def test_calibration_brier_score_perfect() -> None:
    cal = CalibrationTracker(":memory:")
    cal.record("id1", "haiku", 10, "win")   # prob=1.0, outcome=1.0 → (1-1)^2=0
    cal.record("id2", "haiku", 1, "loss")   # prob=0.1, outcome=0.0 → (0.1)^2=0.01
    score = cal.brier_score()
    assert 0.0 <= score < 0.1
    cal.close()


def test_calibration_brier_score_per_agent() -> None:
    cal = CalibrationTracker(":memory:")
    cal.record("id1", "haiku", 7, "win")
    cal.record("id2", "sonnet", 3, "loss")
    haiku_score = cal.brier_score(agent_id="haiku")
    sonnet_score = cal.brier_score(agent_id="sonnet")
    assert haiku_score != sonnet_score
    cal.close()


def test_calibration_table_has_three_buckets() -> None:
    cal = CalibrationTracker(":memory:")
    cal.record("id1", "haiku", 2, "win")
    cal.record("id2", "haiku", 5, "loss")
    cal.record("id3", "haiku", 9, "win")
    table = cal.calibration_table()
    assert len(table) == 3
    assert table[0]["bucket"] == "low (1-3)"
    cal.close()


# ── HaikuAgent SMA/momentum helpers ───────────────────────────────────────────


def test_sma_insufficient_history_returns_none() -> None:
    closes = [Decimal("100")] * 5
    assert _sma(closes, 10) is None


def test_sma_exact_period() -> None:
    closes = [Decimal(str(i)) for i in range(1, 11)]  # 1..10, avg=5.5
    result = _sma(closes, 10)
    assert result is not None
    assert result == pytest.approx(Decimal("5.5"), rel=Decimal("0.001"))


def test_momentum_insufficient_history_returns_none() -> None:
    closes = [Decimal("100")] * 10
    assert _momentum(closes, 14) is None


def test_momentum_positive() -> None:
    closes = [Decimal("100")] * 15
    closes[-1] = Decimal("110")  # 10% gain in 14 days
    result = _momentum(closes, 14)
    assert result is not None
    assert result > Decimal("0")


# ── HaikuAgent integration (mocked LLM) ───────────────────────────────────────


def _make_haiku_agent() -> tuple[HaikuAgent, MagicMock, AgentMemory]:
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.return_value = (_VALID_RESPONSE, _mock_memo())
    mem = AgentMemory(":memory:", AgentId.HAIKU)
    agent = HaikuAgent(llm=mock_llm, memory=mem)
    return agent, mock_llm, mem


def test_haiku_returns_intents_on_valid_response() -> None:
    agent, mock_llm, mem = _make_haiku_agent()
    bars = {sym: _make_bars(sym, 220) for sym in ["SPY", "QQQ"]}
    state = _minimal_state(bars_by_symbol=bars)
    intents = agent.observe(state)
    assert len(intents) == 1
    assert intents[0].symbol == "SPY"
    assert intents[0].conviction == 7
    mem.close()


def test_haiku_returns_empty_on_liquidate_state() -> None:
    agent, _, mem = _make_haiku_agent()
    state = _minimal_state(kill_switch=KillSwitchState.DRAWDOWN_LIQUIDATE)
    intents = agent.observe(state)
    assert intents == []
    mem.close()


def test_haiku_returns_empty_on_llm_failure() -> None:
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.side_effect = BudgetExhausted("over budget")
    mem = AgentMemory(":memory:", AgentId.HAIKU)
    agent = HaikuAgent(llm=mock_llm, memory=mem)
    intents = agent.observe(_minimal_state())
    assert intents == []
    mem.close()


def test_haiku_caps_intents_at_four() -> None:
    big_response = json.dumps({
        "regime_observation": "many signals",
        "intents": [
            {"symbol": f"SYM{i}", "action": "buy", "target_weight": 0.1,
             "sleeve": "equity", "signal": "SMA cross", "conviction": 6,
             "rationale": "trend up"}
            for i in range(6)
        ],
        "next_check": "tomorrow",
    })
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.return_value = (big_response, _mock_memo())
    mem = AgentMemory(":memory:", AgentId.HAIKU)
    agent = HaikuAgent(llm=mock_llm, memory=mem)
    intents = agent.observe(_minimal_state())
    assert len(intents) <= 4
    mem.close()


def test_haiku_returns_empty_on_bad_json() -> None:
    mock_llm = MagicMock(spec=LLMClient)
    mock_llm.call.return_value = ("not json at all", _mock_memo())
    mem = AgentMemory(":memory:", AgentId.HAIKU)
    agent = HaikuAgent(llm=mock_llm, memory=mem)
    intents = agent.observe(_minimal_state())
    assert intents == []
    mem.close()


def test_haiku_records_intents_in_memory() -> None:
    agent, _, mem = _make_haiku_agent()
    bars = {"SPY": _make_bars("SPY", 220)}
    intents = agent.observe(_minimal_state(bars_by_symbol=bars))
    assert len(intents) == 1
    summary = mem.recent_intents_summary(3)
    assert "SPY" in summary
    mem.close()


def test_haiku_equity_trend_in_trend_with_enough_bars() -> None:
    agent, _, mem = _make_haiku_agent()
    bars_by_symbol = {"SPY": _make_bars("SPY", 220, base_price=100.0)}
    trend = agent._compute_equity_trend(bars_by_symbol)
    assert trend["SPY"]["in_trend"] is True
    mem.close()


def test_haiku_equity_trend_out_of_trend_with_insufficient_bars() -> None:
    agent, _, mem = _make_haiku_agent()
    bars_by_symbol = {"SPY": _make_bars("SPY", 50)}
    trend = agent._compute_equity_trend(bars_by_symbol)
    assert trend["SPY"]["in_trend"] is False
    mem.close()


def test_haiku_crypto_trend_in_trend_with_enough_bars() -> None:
    agent, _, mem = _make_haiku_agent()
    bars_by_symbol = {"BTCUSD": _make_bars("BTCUSD", 70, base_price=50000.0)}
    trend = agent._compute_crypto_trend(bars_by_symbol)
    assert trend["BTCUSD"]["in_trend"] is True
    mem.close()
