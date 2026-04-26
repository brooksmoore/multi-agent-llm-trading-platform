"""Tests for ops/journal.py — weekly journals and daily agent memos."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from core.types import AgentId
from ops.journal import write_daily_memo, write_weekly_journal

# ── write_weekly_journal ──────────────────────────────────────────────────────


def test_weekly_journal_creates_correct_filename(tmp_path: Path) -> None:
    # 2026-04-24 is ISO week 17 of 2026
    dest = write_weekly_journal("# Week 17\nSome text.", date(2026, 4, 24), tmp_path)
    assert dest == tmp_path / "WEEK_2026_17.md"
    assert dest.exists()


def test_weekly_journal_content_roundtrip(tmp_path: Path) -> None:
    content = "## Performance\n- SPY +1.2%\n"
    dest = write_weekly_journal(content, date(2026, 4, 24), tmp_path)
    assert dest.read_text(encoding="utf-8") == content


def test_weekly_journal_is_idempotent(tmp_path: Path) -> None:
    """Second call with same week overwrites; does not append."""
    write_weekly_journal("first write", date(2026, 4, 24), tmp_path)
    write_weekly_journal("second write", date(2026, 4, 24), tmp_path)
    content = (tmp_path / "WEEK_2026_17.md").read_text(encoding="utf-8")
    assert content == "second write"


def test_weekly_journal_zero_padded_week(tmp_path: Path) -> None:
    # 2026-01-02 is ISO week 1 of 2026 → should be WEEK_2026_01 (zero-padded)
    dest = write_weekly_journal("", date(2026, 1, 2), tmp_path)
    assert dest.name == "WEEK_2026_01.md"


def test_weekly_journal_creates_logs_dir(tmp_path: Path) -> None:
    nested = tmp_path / "deep" / "logs"
    write_weekly_journal("text", date(2026, 4, 24), nested)
    assert (nested / "WEEK_2026_17.md").exists()


def test_weekly_journal_no_tmp_leftover(tmp_path: Path) -> None:
    write_weekly_journal("abc", date(2026, 4, 24), tmp_path)
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == [], f"Unexpected .tmp files: {tmp_files}"


def test_weekly_journal_empty_content(tmp_path: Path) -> None:
    dest = write_weekly_journal("", date(2026, 4, 24), tmp_path)
    assert dest.exists()
    assert dest.read_text(encoding="utf-8") == ""


# ── write_daily_memo ──────────────────────────────────────────────────────────


def test_daily_memo_creates_correct_path(tmp_path: Path) -> None:
    dest = write_daily_memo("memo text", AgentId.HAIKU, date(2026, 4, 25), tmp_path)
    assert dest == tmp_path / "daily" / "haiku_2026-04-25.md"
    assert dest.exists()


def test_daily_memo_content_roundtrip(tmp_path: Path) -> None:
    content = "- bought SPY\n- sold QQQ\n"
    dest = write_daily_memo(content, AgentId.SONNET, date(2026, 4, 25), tmp_path)
    assert dest.read_text(encoding="utf-8") == content


def test_daily_memo_is_idempotent(tmp_path: Path) -> None:
    write_daily_memo("first", AgentId.OPUS, date(2026, 4, 25), tmp_path)
    write_daily_memo("second", AgentId.OPUS, date(2026, 4, 25), tmp_path)
    content = (tmp_path / "daily" / "opus_2026-04-25.md").read_text(encoding="utf-8")
    assert content == "second"


def test_daily_memo_each_agent_separate_file(tmp_path: Path) -> None:
    d = date(2026, 4, 25)
    write_daily_memo("haiku memo", AgentId.HAIKU, d, tmp_path)
    write_daily_memo("sonnet memo", AgentId.SONNET, d, tmp_path)
    write_daily_memo("opus memo", AgentId.OPUS, d, tmp_path)
    files = {p.name for p in (tmp_path / "daily").iterdir()}
    assert files == {
        "haiku_2026-04-25.md",
        "sonnet_2026-04-25.md",
        "opus_2026-04-25.md",
    }


def test_daily_memo_accepts_string_agent_id(tmp_path: Path) -> None:
    dest = write_daily_memo("text", "manager", date(2026, 4, 25), tmp_path)
    assert dest.name == "manager_2026-04-25.md"


def test_daily_memo_creates_daily_subdir(tmp_path: Path) -> None:
    write_daily_memo("x", AgentId.HAIKU, date(2026, 4, 25), tmp_path)
    assert (tmp_path / "daily").is_dir()


def test_daily_memo_no_tmp_leftover(tmp_path: Path) -> None:
    write_daily_memo("content", AgentId.HAIKU, date(2026, 4, 25), tmp_path)
    tmp_files = list((tmp_path / "daily").glob("*.tmp"))
    assert tmp_files == [], f"Unexpected .tmp files: {tmp_files}"


def test_daily_memo_different_dates_different_files(tmp_path: Path) -> None:
    write_daily_memo("monday", AgentId.SONNET, date(2026, 4, 20), tmp_path)
    write_daily_memo("tuesday", AgentId.SONNET, date(2026, 4, 21), tmp_path)
    assert (tmp_path / "daily" / "sonnet_2026-04-20.md").read_text() == "monday"
    assert (tmp_path / "daily" / "sonnet_2026-04-21.md").read_text() == "tuesday"
