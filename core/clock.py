"""Clock abstraction — wall clock vs. backtest (replay) clock.

The entire codebase should use Clock.now() instead of datetime.utcnow() or
datetime.now() directly. Swapping to BacktestClock makes the same code run
against historical data without any other changes (hexagonal principle).

US market schedule:
  Regular session: 09:30–16:00 ET, Mon–Fri, excluding NYSE holidays.
  Extended hours:  04:00–09:30 ET pre-market; 16:00–20:00 ET after-hours.

We intentionally do NOT import a third-party calendar library here to keep
milestone-1 dependency-free. A lightweight NYSE holiday list is hard-coded
for 2026; the MarketData adapter will provide richer calendar data in M5.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Protocol, runtime_checkable

# ─── US Eastern timezone (no external dep) ────────────────────────────────────
# Python 3.9+ has ZoneInfo; 3.12 ships it. Use it.
try:
    from zoneinfo import ZoneInfo

    ET = ZoneInfo("America/New_York")
except ImportError:
    # Fallback: fixed UTC-5 (non-DST-aware) — acceptable for tests
    ET = timezone(timedelta(hours=-5))  # type: ignore[assignment]


# ─── NYSE holiday list 2026 (extend annually) ─────────────────────────────────
_NYSE_HOLIDAYS_2026: frozenset[date] = frozenset(
    [
        date(2026, 1, 1),   # New Year's Day
        date(2026, 1, 19),  # MLK Day
        date(2026, 2, 16),  # Presidents' Day
        date(2026, 4, 3),   # Good Friday
        date(2026, 5, 25),  # Memorial Day
        date(2026, 6, 19),  # Juneteenth
        date(2026, 7, 3),   # Independence Day (observed)
        date(2026, 9, 7),   # Labor Day
        date(2026, 11, 26), # Thanksgiving
        date(2026, 11, 27), # Black Friday (early close 13:00, treated as half-day)
        date(2026, 12, 24), # Christmas Eve (early close)
        date(2026, 12, 25), # Christmas Day
    ]
)

_NYSE_EARLY_CLOSES_2026: dict[date, int] = {
    date(2026, 11, 27): 13,  # Closes 13:00 ET
    date(2026, 12, 24): 13,
}


# ─── Protocol ─────────────────────────────────────────────────────────────────


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current UTC timestamp."""
        ...

    def now_et(self) -> datetime:
        """Current timestamp in US Eastern time."""
        ...

    def today_et(self) -> date:
        """Current calendar date in US Eastern time."""
        ...

    def is_trading_day(self, d: date | None = None) -> bool:
        """True if d (or today) is a NYSE trading day."""
        ...

    def market_open(self) -> bool:
        """True if the regular session is currently open."""
        ...

    def next_open(self) -> datetime:
        """Next regular-session open in ET."""
        ...


# ─── WallClock ────────────────────────────────────────────────────────────────


class WallClock:
    """Real-time clock backed by system time."""

    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    def now_et(self) -> datetime:
        return datetime.now(tz=ET)

    def today_et(self) -> date:
        return self.now_et().date()

    def is_trading_day(self, d: date | None = None) -> bool:
        target = d if d is not None else self.today_et()
        if target.weekday() >= 5:   # Saturday=5, Sunday=6
            return False
        return target not in _NYSE_HOLIDAYS_2026

    def market_open(self) -> bool:
        now = self.now_et()
        if not self.is_trading_day(now.date()):
            return False
        early_close_hour = _NYSE_EARLY_CLOSES_2026.get(now.date(), 16)
        open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
        close_time = now.replace(hour=early_close_hour, minute=0, second=0, microsecond=0)
        return open_time <= now < close_time

    def next_open(self) -> datetime:
        now = self.now_et()
        candidate = now.date()
        # If today's open hasn't happened yet
        open_today = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now < open_today and self.is_trading_day(candidate):
            return open_today
        # Advance days until we find a trading day
        candidate += timedelta(days=1)
        while not self.is_trading_day(candidate):
            candidate += timedelta(days=1)
        return datetime(
            candidate.year, candidate.month, candidate.day,
            9, 30, tzinfo=ET,
        )


# ─── BacktestClock ────────────────────────────────────────────────────────────


class BacktestClock:
    """Controllable clock for replay / backtesting.

    Thread-safe: advance() / reset() acquire the lock before mutating.
    """

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("BacktestClock start must be timezone-aware")
        self._current: datetime = start.astimezone(UTC)
        self._lock = threading.Lock()

    def now(self) -> datetime:
        with self._lock:
            return self._current

    def now_et(self) -> datetime:
        with self._lock:
            return self._current.astimezone(ET)

    def today_et(self) -> date:
        return self.now_et().date()

    def advance(self, delta: timedelta) -> None:
        with self._lock:
            self._current += delta

    def set(self, dt: datetime) -> None:
        if dt.tzinfo is None:
            raise ValueError("BacktestClock.set() requires timezone-aware datetime")
        with self._lock:
            self._current = dt.astimezone(UTC)

    def is_trading_day(self, d: date | None = None) -> bool:
        target = d if d is not None else self.today_et()
        if target.weekday() >= 5:
            return False
        return target not in _NYSE_HOLIDAYS_2026

    def market_open(self) -> bool:
        now = self.now_et()
        if not self.is_trading_day(now.date()):
            return False
        early_close_hour = _NYSE_EARLY_CLOSES_2026.get(now.date(), 16)
        open_time = now.replace(hour=9, minute=30, second=0, microsecond=0)
        close_time = now.replace(hour=early_close_hour, minute=0, second=0, microsecond=0)
        return open_time <= now < close_time

    def next_open(self) -> datetime:
        now = self.now_et()
        candidate = now.date()
        open_today = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now < open_today and self.is_trading_day(candidate):
            return open_today
        candidate += timedelta(days=1)
        while not self.is_trading_day(candidate):
            candidate += timedelta(days=1)
        return datetime(
            candidate.year, candidate.month, candidate.day,
            9, 30, tzinfo=ET,
        )


# ─── Module-level singleton (replaced by dependency injection in tests) ────────

_default_clock: Clock = WallClock()


def get_clock() -> Clock:
    return _default_clock


def set_clock(clock: Clock) -> None:
    global _default_clock  # noqa: PLW0603
    _default_clock = clock
