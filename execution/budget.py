"""Daily LLM spend ledger.

Enforces the $0.95/day Anthropic API budget cap (DAILY_SPEND_CAP env var).
Persists to data/daily_spend.json so the cap survives restarts.

Usage:
    ledger = BudgetLedger(Path("data/daily_spend.json"))
    ledger.reset_if_new_day(date.today())    # call at market open
    if ledger.is_exhausted():
        kill_engine.trip_budget_exhausted()
    ledger.record_spend(AgentId.HAIKU, Decimal("0.003"), "morning_brief", ts)
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from execution.kill_switch import KillSwitchEngine

log = logging.getLogger(__name__)

DEFAULT_DAILY_LIMIT: Decimal = Decimal("0.95")


class _EntryDict(TypedDict):
    ts: str
    agent_id: str
    call_type: str
    cost_usd: str


class _DayDict(TypedDict):
    date: str
    total_usd: str
    entries: list[_EntryDict]


@dataclass
class SpendEntry:
    ts: datetime
    agent_id: str
    call_type: str
    cost_usd: Decimal


class BudgetLedger:
    """Thread-safe daily spend tracker with JSON persistence.

    The ledger always refers to a single calendar date. On the first call
    to `reset_if_new_day(today)` with a new date, the ledger clears and
    rewrites the backing file.
    """

    def __init__(
        self,
        path: Path,
        daily_limit: Decimal = DEFAULT_DAILY_LIMIT,
    ) -> None:
        self._path = path
        self._limit = daily_limit
        self._lock = threading.Lock()
        self._today: date | None = None
        self._total: Decimal = Decimal("0")
        self._entries: list[_EntryDict] = []
        self._load()

    # ── Write operations ──────────────────────────────────────────────────────

    def record_spend(
        self,
        agent_id: str,
        cost_usd: Decimal,
        call_type: str,
        ts: datetime,
    ) -> None:
        """Record a single LLM call's cost. Auto-advances date if needed."""
        with self._lock:
            self._ensure_date(ts.date())
            self._total += cost_usd
            self._entries.append(
                _EntryDict(
                    ts=ts.isoformat(),
                    agent_id=agent_id,
                    call_type=call_type,
                    cost_usd=str(cost_usd),
                )
            )
            self._flush()

    def reset_if_new_day(self, today: date) -> bool:
        """Reset ledger if `today` differs from the current date.

        Returns True if a reset happened (useful for callers that want to
        log the rollover).
        """
        with self._lock:
            if self._today == today:
                return False
            self._today = today
            self._total = Decimal("0")
            self._entries = []
            self._flush()
            return True

    # ── Read operations ───────────────────────────────────────────────────────

    def today_spent(self) -> Decimal:
        with self._lock:
            return self._total

    def remaining(self) -> Decimal:
        with self._lock:
            return max(self._limit - self._total, Decimal("0"))

    def is_exhausted(self) -> bool:
        with self._lock:
            return self._total >= self._limit

    def daily_limit(self) -> Decimal:
        return self._limit

    def entries(self) -> list[_EntryDict]:
        """Snapshot of today's spend entries (for read-only callers)."""
        with self._lock:
            return list(self._entries)

    # ── Persistence ───────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw: _DayDict = json.loads(self._path.read_text())
            loaded_date = date.fromisoformat(raw["date"])
            if loaded_date != datetime.now(UTC).date():
                return  # stale file — start fresh; don't wipe it yet
            self._today = loaded_date
            self._total = Decimal(raw["total_usd"])
            self._entries = raw["entries"]
        except (json.JSONDecodeError, KeyError, ValueError):
            return  # corrupt file; start fresh on first write

    def _flush(self) -> None:
        """Write current state to disk. Caller must hold self._lock."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        day: _DayDict = {
            "date": str(self._today or date.today()),
            "total_usd": str(self._total),
            "entries": self._entries,
        }
        self._path.write_text(json.dumps(day, indent=2))

    def _ensure_date(self, today: date) -> None:
        """Silently advance to `today` if needed. Caller must hold self._lock."""
        if self._today != today:
            self._today = today
            self._total = Decimal("0")
            self._entries = []


class BudgetWatcher:
    """Polls BudgetLedger and trips KillSwitchEngine when daily spend is exhausted.

    Per blueprint §5 Layer 3: on exhaustion the system degrades to Haiku-only mode.
    The kill switch trip (BUDGET_EXHAUSTED) is what triggers that degradation in app.py.
    """

    def __init__(
        self,
        ledger: BudgetLedger,
        kill: KillSwitchEngine,
        poll_interval_secs: float = 30.0,
    ) -> None:
        self._ledger = ledger
        self._kill = kill
        self._poll_interval = poll_interval_secs
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._tripped = False

    def start(self) -> None:
        """Start the background polling loop."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="budget-watcher"
        )
        self._thread.start()
        log.info("BudgetWatcher: started (poll_interval=%.0fs)", self._poll_interval)

    def stop(self) -> None:
        """Stop the polling loop (waits up to 5s for the thread to exit)."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def check_once(self) -> bool:
        """Synchronous single check. Returns True if budget just tripped the kill switch."""
        if self._tripped:
            return False
        if self._ledger.is_exhausted():
            self._kill.trip_budget_exhausted()
            self._tripped = True
            log.warning(
                "BudgetWatcher: daily spend exhausted ($%.4f >= $%.4f) — "
                "kill switch tripped; system degraded to Haiku-only mode",
                float(self._ledger.today_spent()),
                float(self._ledger.daily_limit()),
            )
            return True
        return False

    def reset(self) -> None:
        """Reset the tripped flag. Call alongside BudgetLedger.reset_if_new_day()."""
        self._tripped = False

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception:
                log.exception("BudgetWatcher: error in poll loop")
            self._stop_event.wait(self._poll_interval)
