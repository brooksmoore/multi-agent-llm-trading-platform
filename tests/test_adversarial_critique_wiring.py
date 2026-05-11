"""Tests for the Sunday adversarial-critique job (T2.4).

Manager.adversarial_critique is mocked; the test exercises App's
_job_manager_sunday_critique end-to-end against in-memory stores.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agents.manager_bridge import KEY_LAST_CRITIQUE
from agents.memory import AgentMemory
from app import JOB_MANAGER_SUNDAY_CRITIQUE, App
from config.settings import Settings
from core.types import AgentId, new_id
from execution.fake_broker import FakeBroker
from tests.test_app_scheduler import _StubMD


@pytest.fixture
def app(tmp_path: Path) -> App:
    settings = Settings(
        alpaca_paper=True, alpaca_api_key="x", alpaca_secret_key="x",
        anthropic_api_key="x", ntfy_topic="",
        master_capability=Decimal("1.0"), daily_spend_cap=Decimal("0.95"),
        data_dir=str(tmp_path / "data"), logs_dir=str(tmp_path / "logs"),
    )
    return App(
        settings, broker=FakeBroker(), market_data=_StubMD(),
        universe=["SPY"], run_dashboard=False, run_volatility_scanner=False,
        run_recover_on_start=False,
    )


def _seed_intent(
    mem: AgentMemory,
    *,
    symbol: str = "NVDA",
    target_weight: str = "0.10",
    conviction: int = 9,
    days_ago: int = 2,
) -> str:
    ts = datetime.now(UTC) - timedelta(days=days_ago)
    intent_id = new_id()
    mem.record_intent(
        intent_id=intent_id, symbol=symbol, action="buy",
        conviction=conviction, rationale="strong setup", ts=ts,
        target_weight=Decimal(target_weight),
    )
    mem.record_outcome(intent_id, "filled")
    return str(intent_id)


def test_critique_skipped_when_no_recent_intents(app: App) -> None:
    """No intents in the last 7d: job logs and returns without calling Manager."""
    app.manager.adversarial_critique = MagicMock()

    app._job_manager_sunday_critique()

    app.manager.adversarial_critique.assert_not_called()


def test_critique_picks_top_intents_and_calls_manager(app: App) -> None:
    """Job pulls top intents per sleeve, calls Manager, persists per-sleeve."""
    _seed_intent(app._memories[AgentId.OPUS], symbol="TSM",
                 target_weight="0.14", conviction=9)
    _seed_intent(app._memories[AgentId.SONNET], symbol="NVDA",
                 target_weight="0.10", conviction=8)

    app.manager.adversarial_critique = MagicMock(return_value={
        "critiques": [
            {
                "agent": "opus",
                "intent_id": "x",
                "summary_of_intent": "Opus TSM 14% conv 9",
                "red_team_objection": "thesis is consensus",
                "what_evidence_would_change_my_mind": "non-consensus angle",
                "severity": "material",
            },
            {
                "agent": "sonnet",
                "intent_id": "y",
                "summary_of_intent": "Sonnet NVDA 10% conv 8",
                "red_team_objection": "factor rank uncertain",
                "what_evidence_would_change_my_mind": "fresh ranking refresh",
                "severity": "minor",
            },
        ],
    })

    app._job_manager_sunday_critique()

    app.manager.adversarial_critique.assert_called_once()
    intents_passed = app.manager.adversarial_critique.call_args.args[1]
    assert len(intents_passed) == 2

    # Each affected sleeve's manager memory now has its critique
    opus_critique = app._memories[AgentId.MANAGER].recall(
        KEY_LAST_CRITIQUE.format(agent="opus"),
    )
    sonnet_critique = app._memories[AgentId.MANAGER].recall(
        KEY_LAST_CRITIQUE.format(agent="sonnet"),
    )
    assert opus_critique and "thesis is consensus" in opus_critique
    assert sonnet_critique and "factor rank uncertain" in sonnet_critique


def test_critique_handles_empty_manager_response(app: App) -> None:
    """Empty critiques: no writes, no error."""
    _seed_intent(app._memories[AgentId.HAIKU], symbol="SPY")
    app.manager.adversarial_critique = MagicMock(return_value={"critiques": []})

    app._job_manager_sunday_critique()  # should not raise

    assert app._memories[AgentId.MANAGER].recall(
        KEY_LAST_CRITIQUE.format(agent="haiku"),
    ) is None


def test_critique_handles_manager_exception(app: App) -> None:
    """Manager.adversarial_critique raises: job logs + returns."""
    _seed_intent(app._memories[AgentId.HAIKU], symbol="SPY")
    app.manager.adversarial_critique = MagicMock(side_effect=RuntimeError("LLM down"))

    app._job_manager_sunday_critique()  # should not raise

    assert app._memories[AgentId.MANAGER].recall(
        KEY_LAST_CRITIQUE.format(agent="haiku"),
    ) is None


def test_critique_top_intents_selection_orders_by_conviction_times_weight(
    tmp_path: Path,
) -> None:
    """top_intents_since picks highest (conviction × target_weight) first."""
    mem = AgentMemory(tmp_path / "haiku.db", AgentId.HAIKU)

    # Three intents with different stake levels
    low_id = _seed_intent(mem, symbol="SPY", target_weight="0.02", conviction=5)
    mid_id = _seed_intent(mem, symbol="QQQ", target_weight="0.10", conviction=7)
    big_id = _seed_intent(mem, symbol="TLT", target_weight="0.18", conviction=9)

    rows = mem.top_intents_since(
        since=datetime.now(UTC) - timedelta(days=14), n=3,
    )

    ids = [r["intent_id"] for r in rows]
    # big (0.18 * 9 = 1.62) > mid (0.10 * 7 = 0.70) > low (0.02 * 5 = 0.10)
    assert ids == [big_id, mid_id, low_id]
    mem.close()


def test_critique_top_intents_excludes_intents_without_outcome(
    tmp_path: Path,
) -> None:
    """Intents that never fired (no outcome) are excluded from critique picks."""
    mem = AgentMemory(tmp_path / "haiku.db", AgentId.HAIKU)

    # One never-fired intent (no outcome)
    never_id = new_id()
    mem.record_intent(
        intent_id=never_id, symbol="SPY", action="buy", conviction=10,
        rationale="x", ts=datetime.now(UTC) - timedelta(days=1),
        target_weight=Decimal("0.20"),
    )
    # One that fired
    fired = _seed_intent(mem, symbol="QQQ", target_weight="0.05", conviction=5)

    rows = mem.top_intents_since(since=datetime.now(UTC) - timedelta(days=7))
    ids = [r["intent_id"] for r in rows]

    assert str(never_id) not in ids
    assert fired in ids
    mem.close()


def test_sunday_critique_job_registered(app: App) -> None:
    """The Sunday cron is in ALL_JOB_IDS and registered at 18:00 ET on Sunday."""
    app._register_jobs()
    job = app.scheduler.get_job(JOB_MANAGER_SUNDAY_CRITIQUE)
    assert job is not None
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert "sun" in fields["day_of_week"]
    assert fields["hour"] == "18"
    assert fields["minute"] == "0"
    app.stop()
