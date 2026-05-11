"""Manager.risk_check wiring tests (T2.1).

Covers the call-site logic in App._maybe_manager_risk_check and the new
risk_check_lite path on ManagerAgent. Mocks the Manager so no live LLM
call happens.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from agents.base import AgentState
from agents.llm import LLMClient
from agents.manager_agent import ManagerAgent
from agents.memory import AgentMemory
from core.types import Action, AgentId, Intent, KillSwitchState, Sleeve, new_id
from execution.broker import BrokerAccount

# ── ManagerAgent.risk_check_lite uses llm_lite when provided ──────────────────


def _intent(conviction: int, target_weight: str) -> Intent:
    return Intent(
        id=new_id(),
        agent_id=AgentId.OPUS,
        symbol="TSM",
        action=Action.BUY,
        target_weight=Decimal(target_weight),
        sleeve=Sleeve.EQUITY,
        signal="momentum cross",
        conviction=conviction,
        rationale="strong thesis",
        timestamp=datetime(2026, 5, 11, 16, 0, tzinfo=UTC),
    )


def _state() -> AgentState:
    return AgentState(
        timestamp=datetime(2026, 5, 11, 16, 0, tzinfo=UTC),
        bars_by_symbol={}, news=[], positions=[],
        account=BrokerAccount(
            cash=Decimal("1000"), equity=Decimal("1000"),
            buying_power=Decimal("1000"),
            pattern_day_trader=False, daytrade_count=0,
        ),
        kill_switch_state=KillSwitchState.OK,
        master_capability=Decimal("1.0"),
        effective_max_gross=Decimal("1.0"),
    )


def _mock_llm(response_json: str) -> MagicMock:
    from agents.llm import HAIKU_MODEL  # noqa: PLC0415
    from core.types import AgentMemo  # noqa: PLC0415

    memo = AgentMemo(
        id=new_id(), agent_id=AgentId.MANAGER, call_type="risk_check",
        model=HAIKU_MODEL, timestamp=datetime.now(UTC),
        cached_tokens=0, new_input_tokens=10, output_tokens=5,
        cost_usd=Decimal("0.001"), prompt_hash="x",
        response_json=response_json, intents_emitted=0,
    )
    client = MagicMock(spec=LLMClient)
    client.call.return_value = (response_json, memo)
    return client


def test_manager_risk_check_uses_main_client_by_default() -> None:
    """risk_check() routes via self._llm (Opus client) by default."""
    main = _mock_llm('{"decision":"approve","downsize_to_weight":null,"reason":"OK"}')
    lite = _mock_llm('{"decision":"approve"}')
    mgr = ManagerAgent(llm=main, memory=AgentMemory(":memory:", AgentId.MANAGER), llm_lite=lite)

    result = mgr.risk_check(_state(), _intent(9, "0.10"))

    assert result["decision"] == "approve"
    main.call.assert_called_once()
    lite.call.assert_not_called()
    # call_type passed to the LLM should be "risk_check"
    assert main.call.call_args.kwargs["call_type"] == "risk_check"


def test_manager_risk_check_lite_uses_lite_client() -> None:
    """risk_check_lite() routes via llm_lite (Sonnet) and uses call_type='risk_check_lite'."""
    main = _mock_llm('{"decision":"veto"}')
    lite = _mock_llm('{"decision":"approve","reason":"OK on lite"}')
    mgr = ManagerAgent(llm=main, memory=AgentMemory(":memory:", AgentId.MANAGER), llm_lite=lite)

    result = mgr.risk_check_lite(_state(), _intent(9, "0.10"))

    assert result["decision"] == "approve"
    lite.call.assert_called_once()
    main.call.assert_not_called()
    assert lite.call.call_args.kwargs["call_type"] == "risk_check_lite"


def test_manager_risk_check_lite_falls_back_to_main_when_lite_unset() -> None:
    """Backward-compat: if llm_lite is omitted, lite calls reuse the main client."""
    main = _mock_llm('{"decision":"approve"}')
    mgr = ManagerAgent(llm=main, memory=AgentMemory(":memory:", AgentId.MANAGER))

    mgr.risk_check_lite(_state(), _intent(9, "0.10"))

    main.call.assert_called_once()


# ── App._maybe_manager_risk_check call-site logic ─────────────────────────────


@pytest.fixture
def app(tmp_path: object) -> object:
    """Build a real App with FakeBroker + stub MarketData. Mocks the Manager."""
    from pathlib import Path  # noqa: PLC0415

    from app import App  # noqa: PLC0415
    from config.settings import Settings  # noqa: PLC0415
    from execution.fake_broker import FakeBroker  # noqa: PLC0415
    from tests.test_app_scheduler import _StubMD  # noqa: PLC0415

    p = Path(str(tmp_path))  # type: ignore[arg-type]
    settings = Settings(
        alpaca_paper=True, alpaca_api_key="x", alpaca_secret_key="x",
        anthropic_api_key="x", ntfy_topic="",
        master_capability=Decimal("1.0"), daily_spend_cap=Decimal("0.95"),
        data_dir=str(p / "data"), logs_dir=str(p / "logs"),
    )
    return App(
        settings, broker=FakeBroker(), market_data=_StubMD(),
        universe=["SPY"], run_dashboard=False, run_volatility_scanner=False,
        run_recover_on_start=False,
    )


def _hi_conviction_intent() -> Intent:
    return _intent(9, "0.10")


def test_low_conviction_skips_manager(app: object) -> None:
    """Conviction < 9: no Manager call, intent passes through unchanged."""
    a = app  # type: ignore[assignment]
    a.manager.risk_check = MagicMock()
    a.manager.risk_check_lite = MagicMock()

    result = a._maybe_manager_risk_check(_intent(8, "0.10"), _state())  # type: ignore[attr-defined]

    assert result is not None and result.conviction == 8
    a.manager.risk_check.assert_not_called()
    a.manager.risk_check_lite.assert_not_called()


def test_small_weight_skips_manager(app: object) -> None:
    """Target weight < 8%: no Manager call."""
    a = app  # type: ignore[assignment]
    a.manager.risk_check = MagicMock()
    a.manager.risk_check_lite = MagicMock()

    result = a._maybe_manager_risk_check(_intent(9, "0.05"), _state())  # type: ignore[attr-defined]

    assert result is not None and result.target_weight == Decimal("0.05")
    a.manager.risk_check.assert_not_called()


def test_veto_returns_none_and_records_outcome(app: object) -> None:
    """decision=veto: returns None, records 'vetoed:manager_risk_check' outcome."""
    a = app  # type: ignore[assignment]
    a.manager.risk_check = MagicMock(return_value={"decision": "veto", "reason": "too crowded"})
    a.outcome_recorder.record = MagicMock()

    result = a._maybe_manager_risk_check(_hi_conviction_intent(), _state())  # type: ignore[attr-defined]

    assert result is None
    a.outcome_recorder.record.assert_called_once()
    args = a.outcome_recorder.record.call_args.args
    assert args[2] == "vetoed:manager_risk_check"


def test_downsize_resizes_target_weight(app: object) -> None:
    """decision=downsize + downsize_to_weight: returns intent with new weight."""
    a = app  # type: ignore[assignment]
    a.manager.risk_check = MagicMock(
        return_value={"decision": "downsize", "downsize_to_weight": 0.04},
    )
    intent = _hi_conviction_intent()

    result = a._maybe_manager_risk_check(intent, _state())  # type: ignore[attr-defined]

    assert result is not None
    assert result.target_weight == Decimal("0.04")
    # Same id — it's a replace(), not a new intent
    assert result.id == intent.id


def test_third_call_in_day_downgrades_to_lite(app: object) -> None:
    """After 2 Opus risk_checks in the same UTC day, the 3rd uses risk_check_lite."""
    a = app  # type: ignore[assignment]
    # Seed two risk_check entries for today via the BudgetLedger.
    now = datetime.now(UTC)
    a.budget.record_spend("manager", Decimal("0.001"), "risk_check", now)
    a.budget.record_spend("manager", Decimal("0.001"), "risk_check", now)

    a.manager.risk_check = MagicMock(return_value={"decision": "approve"})
    a.manager.risk_check_lite = MagicMock(return_value={"decision": "approve"})

    a._maybe_manager_risk_check(_hi_conviction_intent(), _state())  # type: ignore[attr-defined]

    a.manager.risk_check.assert_not_called()
    a.manager.risk_check_lite.assert_called_once()


def test_first_two_calls_use_full_opus(app: object) -> None:
    """The first 2 calls in a day go through the Opus path, not the Sonnet downgrade."""
    a = app  # type: ignore[assignment]
    a.manager.risk_check = MagicMock(return_value={"decision": "approve"})
    a.manager.risk_check_lite = MagicMock(return_value={"decision": "approve"})

    a._maybe_manager_risk_check(_hi_conviction_intent(), _state())  # type: ignore[attr-defined]
    # Simulate the budget ledger being charged for the first call.
    a.budget.record_spend("manager", Decimal("0.001"), "risk_check", datetime.now(UTC))
    a._maybe_manager_risk_check(_hi_conviction_intent(), _state())  # type: ignore[attr-defined]

    assert a.manager.risk_check.call_count == 2
    a.manager.risk_check_lite.assert_not_called()


def test_risk_check_exception_does_not_block_intent(app: object) -> None:
    """If the Manager call raises, the intent passes through (don't block on infra)."""
    a = app  # type: ignore[assignment]
    a.manager.risk_check = MagicMock(side_effect=RuntimeError("network died"))

    result = a._maybe_manager_risk_check(_hi_conviction_intent(), _state())  # type: ignore[attr-defined]

    assert result is not None
    assert result.conviction == 9
