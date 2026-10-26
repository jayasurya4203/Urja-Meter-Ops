"""Cache behaviour: TTL, stale fallback and single-flight loading."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

from app.cache import TTLCache
from app.client import PortalError


def make_cache(max_entries: int = 16):
    clock = {"now": 0.0}
    cache = TTLCache(
        max_entries=max_entries,
        monotonic=lambda: clock["now"],
        now=lambda: datetime(2026, 6, 1, tzinfo=timezone.utc),
    )
    return cache, clock


def test_miss_then_hit_within_ttl():
    cache, _ = make_cache()
    calls = {"count": 0}

    def load():
        calls["count"] += 1
        return "value"

    first = cache.get_or_load("key", 10, load)
    second = cache.get_or_load("key", 10, load)

    assert (first.cache, first.value) == ("miss", "value")
    assert (second.cache, second.value) == ("hit", "value")
    assert calls["count"] == 1
    assert cache.stats() == {"entries": 1, "hits": 1, "misses": 1, "stales": 0}


def test_expired_entry_is_reloaded():
    cache, clock = make_cache()
    cache.get_or_load("key", 10, lambda: "old")
    clock["now"] = 11

    fresh = cache.get_or_load("key", 10, lambda: "new")

    assert fresh.cache == "miss"
    assert fresh.value == "new"


def test_portal_failure_falls_back_to_a_stale_value():
    cache, clock = make_cache()
    cache.get_or_load("key", 10, lambda: "cached")
    clock["now"] = 60

    def broken():
        raise PortalError("portal is down")

    stale = cache.get_or_load("key", 10, broken)

    assert stale.cache == "stale"
    assert stale.value == "cached"
    assert cache.stats()["stales"] == 1


def test_portal_failure_propagates_when_stale_is_disallowed():
    cache, clock = make_cache()
    cache.get_or_load("key", 10, lambda: "cached")
    clock["now"] = 60

    with pytest.raises(PortalError):
        cache.get_or_load("key", 10, lambda: (_ for _ in ()).throw(PortalError("down")), allow_stale=False)


def test_peek_reports_expired_entries_and_invalidate_removes_them():
    cache, clock = make_cache()
    cache.get_or_load("key", 10, lambda: "value")
    clock["now"] = 11

    peeked = cache.peek("key")
    assert peeked is not None
    assert peeked.cache == "stale"

    cache.invalidate("key")
    assert cache.peek("key") is None


def test_single_flight_loads_once_under_concurrency():
    cache, _ = make_cache()
    calls = {"count": 0}
    start = threading.Barrier(3)

    def load():
        calls["count"] += 1
        time.sleep(0.05)
        return "value"

    results = []

    def worker():
        start.wait()
        results.append(cache.get_or_load("key", 10, load))

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls["count"] == 1
    assert {result.cache for result in results} == {"miss", "hit"}


def test_cache_prunes_when_max_entries_is_exceeded():
    cache, _ = make_cache(max_entries=2)
    for index in range(5):
        cache.get_or_load(f"key-{index}", 10, lambda index=index: index)

    assert cache.stats()["entries"] == 2
