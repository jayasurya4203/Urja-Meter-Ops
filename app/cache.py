"""Small in-process TTL cache with single-flight loading and stale fallback."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Generic, Literal, TypeVar

from app.client import PortalError

logger = logging.getLogger("flock.cache")

T = TypeVar("T")
CacheStatus = Literal["hit", "miss", "stale"]


@dataclass(frozen=True)
class Fetched(Generic[T]):
    value: T
    cache: CacheStatus
    fetched_at: datetime


@dataclass
class _Entry:
    value: object
    fetched_at: datetime
    expires_at: float


class TTLCache:
    def __init__(
        self,
        *,
        max_entries: int = 2048,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._entries: dict[str, _Entry] = {}
        # Striped locks keep single-flight behaviour without leaking one lock per
        # arbitrary energy-window key over a long-running process.
        self._key_locks = [threading.Lock() for _ in range(64)]
        self._guard = threading.Lock()
        self._max_entries = max_entries
        self._monotonic = monotonic
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._hits = 0
        self._misses = 0
        self._stales = 0

    def _lock_for(self, key: str) -> threading.Lock:
        return self._key_locks[hash(key) % len(self._key_locks)]

    def get_or_load(
        self,
        key: str,
        ttl_seconds: int,
        loader: Callable[[], T],
        *,
        allow_stale: bool = True,
    ) -> Fetched[T]:
        """Return a fresh value, loading it once even under concurrent misses.

        If the loader fails with a portal error and an expired entry exists, the
        stale value is returned with ``cache="stale"`` instead of failing the
        request. That is the difference between a blip and an outage.
        """
        lock = self._lock_for(key)
        with lock:
            entry = self._entries.get(key)
            if entry is not None and entry.expires_at > self._monotonic():
                with self._guard:
                    self._hits += 1
                return Fetched(
                    value=entry.value,  # type: ignore[arg-type]
                    cache="hit",
                    fetched_at=entry.fetched_at,
                )

            try:
                value = loader()
            except PortalError:
                if allow_stale and entry is not None:
                    with self._guard:
                        self._stales += 1
                    logger.warning("Serving stale cache entry %s after portal failure", key)
                    return Fetched(
                        value=entry.value,  # type: ignore[arg-type]
                        cache="stale",
                        fetched_at=entry.fetched_at,
                    )
                raise

            fetched_at = self._now()
            self._put(key, value, fetched_at, ttl_seconds)
            with self._guard:
                self._misses += 1
            return Fetched(value=value, cache="miss", fetched_at=fetched_at)

    def _put(self, key: str, value: object, fetched_at: datetime, ttl_seconds: int) -> None:
        with self._guard:
            self._entries[key] = _Entry(
                value=value,
                fetched_at=fetched_at,
                expires_at=self._monotonic() + ttl_seconds,
            )
            if len(self._entries) > self._max_entries:
                self._prune()

    def _prune(self) -> None:
        now = self._monotonic()
        expired = [key for key, entry in self._entries.items() if entry.expires_at <= now]
        for key in expired:
            self._entries.pop(key, None)
        if len(self._entries) <= self._max_entries:
            return
        overflow = len(self._entries) - self._max_entries
        oldest = sorted(self._entries.items(), key=lambda item: item[1].fetched_at)[:overflow]
        for key, _ in oldest:
            self._entries.pop(key, None)

    def peek(self, key: str) -> Fetched[T] | None:
        """Inspect an entry without loading; expired entries are still reported."""
        with self._guard:
            entry = self._entries.get(key)
            if entry is None:
                return None
            status: CacheStatus = "hit" if entry.expires_at > self._monotonic() else "stale"
            return Fetched(
                value=entry.value,  # type: ignore[arg-type]
                cache=status,
                fetched_at=entry.fetched_at,
            )

    def invalidate(self, key: str) -> None:
        with self._guard:
            self._entries.pop(key, None)

    def stats(self) -> dict[str, int]:
        with self._guard:
            return {
                "entries": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
                "stales": self._stales,
            }
