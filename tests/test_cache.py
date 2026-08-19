from __future__ import annotations

from app.cache import ScanCache, ScanEntry


def entry(name: str = "a.txt", size: int = 100) -> ScanEntry:
    return ScanEntry(
        name=name, ext=".txt", kind="text", mime="text/plain", original=b"x" * size
    )


def test_round_trip():
    cache = ScanCache(ttl=60, max_bytes=10_000)
    scan_id = cache.put(entry())
    assert cache.get(scan_id).name == "a.txt"
    assert cache.stats() == {"entries": 1, "bytes": 100}


def test_entries_expire():
    cache = ScanCache(ttl=0.0001, max_bytes=10_000)
    scan_id = cache.put(entry())
    import time

    time.sleep(0.01)
    assert cache.get(scan_id) is None
    assert cache.stats()["entries"] == 0


def test_least_recently_used_is_evicted_when_the_byte_cap_is_hit():
    cache = ScanCache(ttl=60, max_bytes=250)
    first = cache.put(entry("first.txt"))
    second = cache.put(entry("second.txt"))
    cache.get(first)  # touching it makes `second` the least recent
    third = cache.put(entry("third.txt"))
    assert cache.get(second) is None
    assert cache.get(first) is not None
    assert cache.get(third) is not None


def test_updating_with_cleaned_bytes_keeps_the_byte_total_honest():
    cache = ScanCache(ttl=60, max_bytes=10_000)
    scan_id = cache.put(entry())
    cache.update(scan_id, b"y" * 40, {"nfkc": True}, {"actions": []})
    stored = cache.get(scan_id)
    assert stored.cleaned == b"y" * 40
    assert stored.options == {"nfkc": True}
    assert stored.clean_report == {"actions": []}
    assert cache.stats()["bytes"] == 140


def test_updating_an_unknown_id_is_a_no_op():
    cache = ScanCache(ttl=60, max_bytes=10_000)
    cache.update("nope", b"data", {})
    assert cache.stats()["entries"] == 0
