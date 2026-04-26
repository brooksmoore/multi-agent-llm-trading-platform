"""Tests for execution/budget.py — daily spend ledger."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from execution.budget import DEFAULT_DAILY_LIMIT, BudgetLedger, BudgetWatcher
from execution.kill_switch import KillSwitchEngine

# ── Helpers ───────────────────────────────────────────────────────────────────


def _ts(d: date, hour: int = 10) -> datetime:
    return datetime(d.year, d.month, d.day, hour, 0, tzinfo=UTC)


DAY1 = date(2026, 4, 24)
DAY2 = date(2026, 4, 25)


# ── Initial state ─────────────────────────────────────────────────────────────


def test_initial_state(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "spend.json")
    assert ledger.today_spent() == Decimal("0")
    assert ledger.remaining() == DEFAULT_DAILY_LIMIT
    assert ledger.is_exhausted() is False


def test_custom_limit(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "spend.json", daily_limit=Decimal("1.00"))
    assert ledger.daily_limit() == Decimal("1.00")
    assert ledger.remaining() == Decimal("1.00")


# ── record_spend ──────────────────────────────────────────────────────────────


def test_record_spend_increases_total(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "spend.json")
    ledger.reset_if_new_day(DAY1)
    ledger.record_spend("haiku", Decimal("0.01"), "morning_brief", _ts(DAY1))
    assert ledger.today_spent() == Decimal("0.01")
    assert ledger.remaining() == DEFAULT_DAILY_LIMIT - Decimal("0.01")


def test_multiple_spends_accumulate(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "spend.json")
    ledger.reset_if_new_day(DAY1)
    ledger.record_spend("haiku",  Decimal("0.10"), "brief", _ts(DAY1))
    ledger.record_spend("sonnet", Decimal("0.20"), "brief", _ts(DAY1, 11))
    ledger.record_spend("opus",   Decimal("0.30"), "deep_dive", _ts(DAY1, 12))
    assert ledger.today_spent() == Decimal("0.60")


def test_is_exhausted_after_limit_reached(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "spend.json", daily_limit=Decimal("0.50"))
    ledger.reset_if_new_day(DAY1)
    ledger.record_spend("haiku", Decimal("0.50"), "brief", _ts(DAY1))
    assert ledger.is_exhausted() is True


def test_remaining_floors_at_zero(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "spend.json", daily_limit=Decimal("0.10"))
    ledger.reset_if_new_day(DAY1)
    ledger.record_spend("haiku", Decimal("0.20"), "brief", _ts(DAY1))
    assert ledger.remaining() == Decimal("0")  # floored, not negative


# ── reset_if_new_day ──────────────────────────────────────────────────────────


def test_reset_on_new_day_clears_total(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "spend.json")
    ledger.reset_if_new_day(DAY1)
    ledger.record_spend("haiku", Decimal("0.50"), "brief", _ts(DAY1))
    assert ledger.today_spent() == Decimal("0.50")

    reset = ledger.reset_if_new_day(DAY2)
    assert reset is True
    assert ledger.today_spent() == Decimal("0")
    assert ledger.is_exhausted() is False


def test_reset_same_day_returns_false(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "spend.json")
    ledger.reset_if_new_day(DAY1)
    ledger.record_spend("haiku", Decimal("0.10"), "brief", _ts(DAY1))
    reset = ledger.reset_if_new_day(DAY1)
    assert reset is False
    assert ledger.today_spent() == Decimal("0.10")  # not wiped


# ── Persistence ───────────────────────────────────────────────────────────────


def test_spend_is_persisted_to_disk(tmp_path: Path) -> None:
    path = tmp_path / "spend.json"
    now = datetime.now(UTC)
    ledger = BudgetLedger(path)
    ledger.reset_if_new_day(now.date())
    ledger.record_spend("haiku", Decimal("0.05"), "brief", now)
    assert path.exists()


def test_new_instance_loads_same_day_data(tmp_path: Path) -> None:
    path = tmp_path / "spend.json"
    now = datetime.now(UTC)
    today_utc = now.date()
    ledger1 = BudgetLedger(path)
    ledger1.reset_if_new_day(today_utc)
    ledger1.record_spend("haiku", Decimal("0.07"), "brief", now)

    ledger2 = BudgetLedger(path)
    assert ledger2.today_spent() == Decimal("0.07")


def test_stale_date_file_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / "spend.json"
    # Write a file dated yesterday
    import json
    yesterday = date(2026, 4, 23)
    path.write_text(json.dumps({
        "date": str(yesterday),
        "total_usd": "0.88",
        "entries": [],
    }))
    ledger = BudgetLedger(path)
    # Stale date → starts at 0 for today
    assert ledger.today_spent() == Decimal("0")


def test_corrupt_file_starts_fresh(tmp_path: Path) -> None:
    path = tmp_path / "spend.json"
    path.write_text("not valid json{{")
    ledger = BudgetLedger(path)
    assert ledger.today_spent() == Decimal("0")
    assert ledger.is_exhausted() is False


# ── BudgetWatcher ─────────────────────────────────────────────────────────────


def test_budget_watcher_trips_kill_switch_when_exhausted(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "spend.json", daily_limit=Decimal("0.10"))
    kill = KillSwitchEngine()
    watcher = BudgetWatcher(ledger, kill)

    ledger.reset_if_new_day(DAY1)
    ledger.record_spend("haiku", Decimal("0.10"), "brief", _ts(DAY1))
    assert ledger.is_exhausted() is True

    tripped = watcher.check_once()
    assert tripped is True
    from core.types import KillSwitchState  # noqa: PLC0415
    assert kill.state == KillSwitchState.BUDGET_EXHAUSTED


def test_budget_watcher_no_trip_when_not_exhausted(tmp_path: Path) -> None:
    ledger = BudgetLedger(tmp_path / "spend.json", daily_limit=Decimal("0.95"))
    kill = KillSwitchEngine()
    watcher = BudgetWatcher(ledger, kill)

    ledger.reset_if_new_day(DAY1)
    ledger.record_spend("haiku", Decimal("0.10"), "brief", _ts(DAY1))

    tripped = watcher.check_once()
    assert tripped is False
    from core.types import KillSwitchState  # noqa: PLC0415
    assert kill.state == KillSwitchState.OK


def test_budget_watcher_check_once_idempotent_after_trip(tmp_path: Path) -> None:
    """check_once() returns False on subsequent calls after the initial trip."""
    ledger = BudgetLedger(tmp_path / "spend.json", daily_limit=Decimal("0.10"))
    kill = KillSwitchEngine()
    watcher = BudgetWatcher(ledger, kill)

    ledger.reset_if_new_day(DAY1)
    ledger.record_spend("haiku", Decimal("0.10"), "brief", _ts(DAY1))

    assert watcher.check_once() is True   # first call: trips
    assert watcher.check_once() is False  # already tripped, no-op
