"""Tests for the _RateLimitFilter that tames the Alpaca websocket log storm."""

from __future__ import annotations

import logging

from app import _RateLimitFilter


def _rec(msg: str, name: str = "alpaca.trading.stream") -> logging.LogRecord:
    return logging.LogRecord(name, logging.ERROR, "x", 1, msg, None, None)


def test_collapses_identical_records() -> None:
    f = _RateLimitFilter(min_interval_s=60.0)
    msg = "error during websocket communication: [Errno 8] nodename nor servname"
    passed = sum(1 for _ in range(1000) if f.filter(_rec(msg)))
    assert passed == 1  # only the first in the window


def test_distinct_messages_tracked_independently() -> None:
    f = _RateLimitFilter(min_interval_s=60.0)
    assert f.filter(_rec("error A")) is True
    assert f.filter(_rec("error B")) is True   # different message, not suppressed
    assert f.filter(_rec("error A")) is False  # repeat of A within window


def test_window_expiry_lets_one_through(monkeypatch) -> None:  # noqa: ANN001
    f = _RateLimitFilter(min_interval_s=10.0)
    t = [1000.0]
    monkeypatch.setattr("app.time.monotonic", lambda: t[0])
    assert f.filter(_rec("x")) is True
    assert f.filter(_rec("x")) is False
    t[0] += 11.0  # advance past the interval
    assert f.filter(_rec("x")) is True


def test_suppressed_count_annotated() -> None:
    f = _RateLimitFilter(min_interval_s=0.0)  # every call is a new window
    f.filter(_rec("boom"))
    # min_interval 0 means each passes; force a suppression window instead:
    f2 = _RateLimitFilter(min_interval_s=10_000.0)
    f2.filter(_rec("boom"))
    for _ in range(4):
        f2.filter(_rec("boom"))
    # Next window emission would carry the suppressed count; simulate by
    # checking the internal counter rather than time-travel here.
    assert f2._suppressed.get("alpaca.trading.stream:boom") == 4
