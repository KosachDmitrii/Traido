"""Desk revision bus — ETag fingerprint + SSE fan-out."""

from __future__ import annotations

import asyncio
import itertools
from threading import Lock
from typing import Any


class DeskBus:
    def __init__(self) -> None:
        self._lock = Lock()
        self._rev = itertools.count(1)
        self._desk_rev = 0
        self._broker_rev = 0
        self._subs: list[asyncio.Queue[dict[str, Any]]] = []
        self._closing = False

    @property
    def desk_rev(self) -> int:
        return self._desk_rev

    @property
    def broker_rev(self) -> int:
        return self._broker_rev

    def bump_desk(self, kind: str = "desk", **payload: Any) -> int:
        with self._lock:
            self._desk_rev = next(self._rev)
            rev = self._desk_rev
            event = {"type": kind, "channel": "desk", "rev": rev, **payload}
            subs = list(self._subs)
        self._fanout(subs, event)
        return rev

    def bump_broker(self, kind: str = "broker", **payload: Any) -> int:
        with self._lock:
            self._broker_rev = next(self._rev)
            rev = self._broker_rev
            event = {"type": kind, "channel": "broker", "rev": rev, **payload}
            subs = list(self._subs)
        self._fanout(subs, event)
        return rev

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    @property
    def closing(self) -> bool:
        return self._closing

    def close(self) -> None:
        """Tell every open stream to finish, and wake it up to hear it.

        A graceful shutdown waits for in-flight responses, and an SSE stream is
        an in-flight response designed never to end. Without an explicit signal
        the server waits for the stream while the stream waits for the browser,
        so the process only exits when the last tab closes — which under
        `--reload` means every code change hangs.

        The sentinel matters as much as the flag: a stream parked on `q.get()`
        would otherwise not notice for a full keepalive interval.
        """
        self._closing = True
        with self._lock:
            subs = list(self._subs)
        self._fanout(subs, {"type": "closing", "channel": "desk"})

    def reopen(self) -> None:
        """Allow streams again. The bus is a singleton and outlives one app."""
        self._closing = False

    def _fanout(self, subs: list[asyncio.Queue[dict[str, Any]]], event: dict[str, Any]) -> None:
        for q in subs:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass


DESK_BUS = DeskBus()
