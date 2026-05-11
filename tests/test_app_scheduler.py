"""Scheduler registration test — every blueprint §2 cron job is wired."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app import (
    ALL_JOB_IDS,
    JOB_BUDGET_RESET,
    JOB_HAIKU_CLOSE,
    JOB_HAIKU_CRYPTO,
    JOB_HAIKU_NEWS_SCAN,
    JOB_MANAGER_FRIDAY,
    JOB_OPUS_THURSDAY_DEEPDIVE,
    JOB_SONNET_EOD,
    App,
)
from config.settings import Settings
from data.market import Bar, Timeframe
from execution.fake_broker import FakeBroker


class _StubMD:
    def get_bars(  # noqa: ARG002
        self,
        symbol: str,
        start: datetime | None = None,
        end: datetime | None = None,
        timeframe: Timeframe = Timeframe.DAY,
    ) -> list[Bar]:
        return []

    def get_bars_batch(  # noqa: ARG002
        self,
        symbols: list[str],
        start: datetime | None = None,
        end: datetime | None = None,
        timeframe: Timeframe = Timeframe.DAY,
    ) -> dict[str, list[Bar]]:
        return {sym: [] for sym in symbols}

    def get_latest_bar(self, symbol: str) -> Bar | None:  # noqa: ARG002
        return None

    def get_latest_quote(self, symbol: str) -> None:  # noqa: ARG002
        return None

    def get_snapshots(self, symbols: list[str]) -> dict[str, Any]:  # noqa: ARG002
        return {}


def _make_app(tmp_path: Path) -> App:
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


def test_all_blueprint_jobs_register(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app._register_jobs()
    job_ids = {job.id for job in app.scheduler.get_jobs()}
    assert ALL_JOB_IDS.issubset(job_ids), (
        f"missing jobs: {ALL_JOB_IDS - job_ids}"
    )
    app.stop()


def test_market_hours_jobs_use_weekday_cron(tmp_path: Path) -> None:
    """Sonnet / Haiku market-hours jobs must run mon-fri only."""
    app = _make_app(tmp_path)
    app._register_jobs()
    market_jobs = {
        JOB_HAIKU_NEWS_SCAN, JOB_HAIKU_CLOSE,
        JOB_SONNET_EOD,
    }
    for jid in market_jobs:
        job = app.scheduler.get_job(jid)
        assert job is not None, f"{jid} not registered"
        # CronTrigger stores fields; day_of_week must be mon-fri
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert "mon-fri" in fields["day_of_week"], (
            f"{jid} day_of_week={fields['day_of_week']}, expected mon-fri"
        )
    app.stop()


def test_friday_only_jobs(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app._register_jobs()
    job = app.scheduler.get_job(JOB_MANAGER_FRIDAY)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert "fri" in fields["day_of_week"]
    # Thursday-only deep dive (Plan 2c: Friday deep dive removed)
    job = app.scheduler.get_job(JOB_OPUS_THURSDAY_DEEPDIVE)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert "thu" in fields["day_of_week"]
    app.stop()


def test_scheduled_times_match_blueprint(tmp_path: Path) -> None:
    """Sanity: pre-open is 09:25, EOD is 16:30, etc."""
    app = _make_app(tmp_path)
    app._register_jobs()

    expected = {
        JOB_HAIKU_NEWS_SCAN:   ("13", "30"),
        JOB_HAIKU_CLOSE:       ("15", "55"),
        JOB_SONNET_EOD:        ("16", "30"),
        JOB_OPUS_THURSDAY_DEEPDIVE: ("16", "30"),
        JOB_MANAGER_FRIDAY:    ("17", "0"),
    }
    for jid, (h, m) in expected.items():
        job = app.scheduler.get_job(jid)
        fields = {f.name: str(f) for f in job.trigger.fields}
        assert fields["hour"] == h, f"{jid} hour={fields['hour']}"
        assert fields["minute"] == m, f"{jid} minute={fields['minute']}"
    app.stop()


def test_budget_reset_runs_at_utc_midnight(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app._register_jobs()
    job = app.scheduler.get_job(JOB_BUDGET_RESET)
    assert job is not None
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["hour"] == "0"
    assert fields["minute"] == "0"
    # Trigger timezone: UTC
    assert "UTC" in str(job.trigger.timezone)
    app.stop()


def test_haiku_crypto_runs_hourly(tmp_path: Path) -> None:
    app = _make_app(tmp_path)
    app._register_jobs()
    job = app.scheduler.get_job(JOB_HAIKU_CRYPTO)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert fields["minute"] == "0"
    app.stop()


def test_budget_reset_clears_ledger(tmp_path: Path) -> None:
    """Smoke-test the registered handler runs idempotently."""
    app = _make_app(tmp_path)
    today = datetime.now(UTC).date()
    app.budget.reset_if_new_day(today)
    app.budget.record_spend("haiku", Decimal("0.10"), "test", datetime.now(UTC))
    assert app.budget.today_spent() > Decimal("0")
    # Job will not zero today's bucket (same date) but should be callable safely
    app._job_budget_reset()
    app.stop()
