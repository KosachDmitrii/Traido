"""Cached values that can say how old they are and what they were computed from.

A large universe makes caching necessary and makes silent staleness dangerous:
the difference between "no earnings this week" and "we last asked on Friday" is
invisible once a value is stored as a bare result. Every entry here therefore
carries when it was computed, what event time the underlying data belongs to,
when it expires, and a version of the inputs it was derived from.

The input version is the part that is easy to leave out and expensive to omit.
A cached eligibility verdict computed under one policy must not survive a change
to that policy; without a version it would, and the desk would enforce
yesterday's rules while showing today's configuration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from threading import Lock


@dataclass(frozen=True)
class Cached[T]:
    """A value with its provenance attached."""

    value: T
    computed_at: float
    """Monotonic clock. Immune to a wall-clock adjustment mid-session."""

    expires_at: float
    input_version: str
    source_event_time: datetime | None = None
    """When the underlying data happened, as distinct from when we read it.

    A daily bar read at noon belongs to yesterday's close. Freshness questions
    about the *data* have to be asked of this, not of `computed_at`.
    """

    def age_sec(self, now: float | None = None) -> float:
        return (now if now is not None else time.monotonic()) - self.computed_at

    def is_fresh(self, *, input_version: str, now: float | None = None) -> bool:
        if input_version != self.input_version:
            return False
        return (now if now is not None else time.monotonic()) < self.expires_at


class FreshnessCache[T]:
    """A tiny TTL cache that refuses to answer with the wrong-version value.

    Deliberately not `functools.lru_cache`: that would key on arguments and have
    no way to express "this is stale" or "this was computed under a policy that
    has since changed", which are the two questions that matter here.
    """

    def __init__(self) -> None:
        self._entries: dict[str, Cached[T]] = {}
        self._lock = Lock()

    def get(self, key: str, *, input_version: str) -> Cached[T] | None:
        with self._lock:
            entry = self._entries.get(key)
        if entry is None or not entry.is_fresh(input_version=input_version):
            return None
        return entry

    def put(
        self,
        key: str,
        value: T,
        *,
        ttl_sec: float,
        input_version: str,
        source_event_time: datetime | None = None,
    ) -> Cached[T]:
        now = time.monotonic()
        entry = Cached(
            value=value,
            computed_at=now,
            expires_at=now + ttl_sec,
            input_version=input_version,
            source_event_time=source_event_time,
        )
        with self._lock:
            self._entries[key] = entry
        return entry

    def peek(self, key: str) -> Cached[T] | None:
        """The entry regardless of freshness — for reporting, never for deciding."""
        with self._lock:
            return self._entries.get(key)

    def invalidate(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)
