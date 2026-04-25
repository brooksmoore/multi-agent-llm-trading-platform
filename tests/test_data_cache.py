"""Tests for data/cache.py — diskcache wrapper."""

from __future__ import annotations

from pathlib import Path

from data.cache import Cache


def test_get_missing_returns_none(tmp_path: Path) -> None:
    cache = Cache(directory=tmp_path)
    assert cache.get("missing") is None
    cache.close()


def test_set_and_get(tmp_path: Path) -> None:
    cache = Cache(directory=tmp_path)
    cache.set("key", "value")
    assert cache.get("key") == "value"
    cache.close()


def test_set_overwrite(tmp_path: Path) -> None:
    cache = Cache(directory=tmp_path)
    cache.set("key", 1)
    cache.set("key", 2)
    assert cache.get("key") == 2
    cache.close()


def test_delete(tmp_path: Path) -> None:
    cache = Cache(directory=tmp_path)
    cache.set("key", "value")
    cache.delete("key")
    assert cache.get("key") is None
    cache.close()


def test_clear(tmp_path: Path) -> None:
    cache = Cache(directory=tmp_path)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    cache.clear()
    assert cache.get("a") is None
    assert cache.get("b") is None
    assert cache.get("c") is None
    cache.close()


def test_cached_decorator_caches_result(tmp_path: Path) -> None:
    cache = Cache(directory=tmp_path)
    call_count = 0

    @cache.cached()
    def expensive(x: int) -> int:
        nonlocal call_count
        call_count += 1
        return x * 2

    result1 = expensive(5)
    result2 = expensive(5)
    assert result1 == 10
    assert result2 == 10
    assert call_count == 1
    cache.close()


def test_cached_decorator_different_args(tmp_path: Path) -> None:
    cache = Cache(directory=tmp_path)
    call_count = 0

    @cache.cached()
    def double(x: int) -> int:
        nonlocal call_count
        call_count += 1
        return x * 2

    assert double(1) == 2
    assert double(2) == 4
    assert call_count == 2
    cache.close()


def test_cache_close_and_reopen(tmp_path: Path) -> None:
    cache = Cache(directory=tmp_path)
    cache.set("persistent", "hello")
    cache.close()

    cache2 = Cache(directory=tmp_path)
    assert cache2.get("persistent") == "hello"
    cache2.close()
