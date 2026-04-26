"""Persistence helpers for Manager weekly journals and per-agent daily memos.

Weekly journals  → logs/WEEK_{YYYY}_{WW:02d}.md   (ISO week numbering)
Daily agent memos → logs/daily/{agent}_{YYYY-MM-DD}.md

Both writes are idempotent (overwrite on re-run) and atomic (write to a .tmp
file in the same directory, then os.replace() via Path.replace()).

Empty content is still written so callers can detect that a memo was produced
(even if the LLM returned an empty string due to budget exhaustion).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from core.types import AgentId

log = logging.getLogger(__name__)


def write_weekly_journal(markdown: str, ref_date: date, logs_dir: Path) -> Path:
    """Persist the Manager's weekly journal markdown.

    The ISO week that contains *ref_date* determines the filename.
    Returns the path that was written.
    """
    iso = ref_date.isocalendar()
    year, week = iso.year, iso.week
    dest = logs_dir / f"WEEK_{year}_{week:02d}.md"
    _write_atomic(dest, markdown)
    log.info("journal: wrote weekly journal → %s (%d chars)", dest.name, len(markdown))
    return dest


def write_daily_memo(
    content: str,
    agent_id: AgentId | str,
    ref_date: date,
    logs_dir: Path,
) -> Path:
    """Persist a daily agent memo.

    The file is placed under *logs_dir*/daily/ so weekly journals and daily
    memos live in separate subdirectories.  Returns the path that was written.
    """
    agent_str = agent_id.value if isinstance(agent_id, AgentId) else str(agent_id)
    date_str = ref_date.isoformat()
    dest = logs_dir / "daily" / f"{agent_str}_{date_str}.md"
    _write_atomic(dest, content)
    log.info("journal: wrote daily memo → %s/%s (%d chars)", "daily", dest.name, len(content))
    return dest


# ── Internal ──────────────────────────────────────────────────────────────────


def _write_atomic(path: Path, content: str) -> None:
    """Write *content* to *path* atomically via a sibling .tmp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
