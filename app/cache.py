"""In-memory store for scan results, so pressing Remove does not re-upload.

Deliberately memory-only: uploads are never written to disk, and the container
runs with a read-only root filesystem to keep it that way. Entries expire on a
TTL and the store is byte-capped with least-recently-used eviction, so a big
session cannot grow without bound.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ScanEntry:
    """One scanned item, held between the scan and an optional clean."""

    name: str
    ext: str
    kind: str
    mime: str
    original: bytes
    #: Cleaned bytes under the options used at scan time. Recomputed if the
    #: user changes options before removing.
    cleaned: bytes | None = None
    options: dict[str, Any] = field(default_factory=dict)
    report: Any = None
    #: The report the engine returned for the clean that produced `cleaned`.
    clean_report: Any = None
    created: float = field(default_factory=time.monotonic)

    @property
    def size(self) -> int:
        return len(self.original) + (len(self.cleaned) if self.cleaned else 0)


class ScanCache:
    """Thread-safe TTL + byte-capped LRU cache keyed by an opaque scan id."""

    def __init__(self, ttl: float, max_bytes: int) -> None:
        self._ttl = ttl
        self._max_bytes = max_bytes
        self._items: OrderedDict[str, ScanEntry] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def put(self, entry: ScanEntry) -> str:
        scan_id = secrets.token_urlsafe(16)
        with self._lock:
            self._expire_locked()
            self._items[scan_id] = entry
            self._bytes += entry.size
            self._evict_locked()
        return scan_id

    def get(self, scan_id: str) -> ScanEntry | None:
        with self._lock:
            self._expire_locked()
            entry = self._items.get(scan_id)
            if entry is not None:
                self._items.move_to_end(scan_id)
            return entry

    def update(
        self,
        scan_id: str,
        cleaned: bytes,
        options: dict[str, Any],
        clean_report: Any = None,
    ) -> None:
        """Attach cleaned bytes to an entry, keeping the byte total honest."""
        with self._lock:
            entry = self._items.get(scan_id)
            if entry is None:
                return
            self._bytes -= entry.size
            entry.cleaned = cleaned
            entry.options = dict(options)
            entry.clean_report = clean_report
            self._bytes += entry.size
            self._items.move_to_end(scan_id)
            self._evict_locked()

    def drop(self, scan_id: str) -> None:
        with self._lock:
            entry = self._items.pop(scan_id, None)
            if entry is not None:
                self._bytes -= entry.size

    def stats(self) -> dict[str, int]:
        with self._lock:
            self._expire_locked()
            return {"entries": len(self._items), "bytes": self._bytes}

    # -- internals (call with the lock held) ---------------------------------

    def _expire_locked(self) -> None:
        if self._ttl <= 0:
            return
        cutoff = time.monotonic() - self._ttl
        stale = [k for k, v in self._items.items() if v.created < cutoff]
        for key in stale:
            entry = self._items.pop(key)
            self._bytes -= entry.size

    def _evict_locked(self) -> None:
        while self._bytes > self._max_bytes and self._items:
            _, entry = self._items.popitem(last=False)
            self._bytes -= entry.size
