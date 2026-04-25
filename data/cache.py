"""Thin diskcache wrapper with TTL-based caching and a cached() decorator."""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import diskcache

F = TypeVar("F", bound=Callable[..., Any])


class Cache:
    def __init__(
        self, directory: str | Path = ".cache", default_ttl: int = 3600
    ) -> None:
        self._cache = diskcache.Cache(str(directory))
        self._default_ttl = default_ttl

    def get(self, key: str) -> Any | None:
        return self._cache.get(key)

    def set(self, key: str, value: Any, ttl: int | None = None) -> None:
        expire = ttl if ttl is not None else self._default_ttl
        self._cache.set(key, value, expire=expire)

    def delete(self, key: str) -> None:
        self._cache.delete(key)

    def clear(self) -> None:
        self._cache.clear()

    def close(self) -> None:
        self._cache.close()

    def cached(self, ttl: int | None = None) -> Callable[[F], F]:
        def decorator(func: F) -> F:
            @functools.wraps(func)
            def wrapper(*args: Any, **kwargs: Any) -> Any:
                key = f"{func.__qualname__}:{args!r}:{sorted(kwargs.items())!r}"
                cached_val = self.get(key)
                if cached_val is not None:
                    return cached_val
                result = func(*args, **kwargs)
                self.set(key, result, ttl=ttl)
                return result

            return wrapper  # type: ignore[return-value]

        return decorator
