"""Tests for the token-bucket limiter in data/news_fetcher.py."""

from __future__ import annotations

import time

from data.news_fetcher import _RateLimiter


def test_under_limit_does_not_sleep() -> None:
    sleeps: list[float] = []
    rl = _RateLimiter(max_calls=5, window_s=60.0, sleep=sleeps.append)
    for _ in range(5):
        rl.acquire()
    assert sleeps == []


def test_at_limit_blocks_until_window_clears() -> None:
    sleeps: list[float] = []

    def fake_sleep(s: float) -> None:
        sleeps.append(s)
        # Advance time inside the limiter's view by purging old calls — easier
        # to just rely on real clock with a small window.

    # Use a tiny window so we can actually block on the real clock.
    rl = _RateLimiter(max_calls=2, window_s=0.05)
    t0 = time.monotonic()
    for _ in range(3):
        rl.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.05, f"third call should have waited, elapsed={elapsed:.4f}s"


def test_zero_max_disables_limiter() -> None:
    rl = _RateLimiter(max_calls=0, window_s=60.0)
    for _ in range(1000):
        rl.acquire()  # must not block or hang
