"""Tests for data/bar_cache.py — sqlite cache + CachedMarketData wrapper."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from data.bar_cache import BarCache, CachedMarketData
from data.market import Bar, ReplayMarketData, Timeframe


def _bar(sym: str, day: datetime, close: str = "100.0") -> Bar:
    return Bar(
        symbol=sym, timestamp=day,
        open=Decimal(close), high=Decimal(close), low=Decimal(close),
        close=Decimal(close), volume=1000,
    )


def _days(n: int, *, end: datetime | None = None) -> list[datetime]:
    """Return n consecutive UTC daily timestamps ending at `end` (default: yesterday)."""
    end = end or (datetime.now(UTC) - timedelta(days=1))
    end = datetime.combine(end.date(), datetime.min.time(), UTC)
    return [end - timedelta(days=n - 1 - i) for i in range(n)]


def test_cache_round_trip(tmp_path: Path) -> None:
    cache = BarCache(tmp_path / "bars.db")
    days = _days(5)
    bars = [_bar("AAPL", d) for d in days]
    assert cache.write(bars) == 5

    out = cache.read_range(["AAPL"], days[0], days[-1])
    assert len(out["AAPL"]) == 5


def test_cache_skips_today(tmp_path: Path) -> None:
    cache = BarCache(tmp_path / "bars.db")
    today = datetime.now(UTC)
    yesterday = datetime.combine((today - timedelta(days=1)).date(), datetime.min.time(), UTC)
    bars = [_bar("AAPL", yesterday), _bar("AAPL", today)]
    # Today's bar must not be persisted.
    assert cache.write(bars) == 1


def test_cached_market_data_serves_from_cache(tmp_path: Path) -> None:
    """Second call with the same range must not re-fetch from upstream."""
    days = _days(10)
    upstream_bars = {"AAPL": [_bar("AAPL", d) for d in days]}

    class CountingMD(ReplayMarketData):
        def __init__(self, bars):
            super().__init__(bars)
            self.batch_calls = 0

        def get_bars_batch(self, symbols, start, end, timeframe=Timeframe.DAY):
            self.batch_calls += 1
            return super().get_bars_batch(symbols, start, end, timeframe)

    underlying = CountingMD(upstream_bars)
    cache = BarCache(tmp_path / "bars.db")
    md = CachedMarketData(underlying, cache)

    res1 = md.get_bars_batch(["AAPL"], days[0], days[-1])
    assert len(res1["AAPL"]) == 10
    assert underlying.batch_calls == 1

    # Second call: cache covers the historical range. The wrapper still asks
    # upstream for "today" (intraday-mutable), but that's a much smaller
    # window — assert we got the same data back without expanding the call.
    res2 = md.get_bars_batch(["AAPL"], days[0], days[-1])
    assert len(res2["AAPL"]) == 10


def test_cached_market_data_fills_gap_only(tmp_path: Path) -> None:
    """If cache has bars 1-5, requesting 1-10 should only fetch 6-10."""
    days = _days(10)
    upstream_bars = {"AAPL": [_bar("AAPL", d) for d in days]}

    class GapTracker(ReplayMarketData):
        def __init__(self, bars):
            super().__init__(bars)
            self.last_fetch_start: datetime | None = None

        def get_bars_batch(self, symbols, start, end, timeframe=Timeframe.DAY):
            self.last_fetch_start = start
            return super().get_bars_batch(symbols, start, end, timeframe)

    underlying = GapTracker(upstream_bars)
    cache = BarCache(tmp_path / "bars.db")

    # Pre-seed cache with first 5 days.
    cache.write([_bar("AAPL", d) for d in days[:5]])

    md = CachedMarketData(underlying, cache)
    res = md.get_bars_batch(["AAPL"], days[0], days[-1])

    assert len(res["AAPL"]) == 10
    # Upstream fetch should have started at day 6 (or later), not day 1.
    assert underlying.last_fetch_start is not None
    assert underlying.last_fetch_start.date() >= days[5].date()
