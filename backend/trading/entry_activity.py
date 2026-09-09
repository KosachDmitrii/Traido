"""Process-local entry ownership; persisted intents own restart recovery."""

from collections import Counter
from collections.abc import Awaitable, Callable
from functools import wraps
from threading import RLock
from typing import Concatenate, cast
from uuid import UUID

_lock = RLock()
_active: Counter[UUID | None] = Counter()


def entry_active(opportunity_id: UUID | None) -> bool:
    with _lock:
        return _active[opportunity_id] > 0


def track_entry[S, R, **P](
    function: Callable[Concatenate[S, UUID, P], Awaitable[R]],
) -> Callable[Concatenate[S, UUID, P], Awaitable[R]]:
    @wraps(function)
    async def wrapped(self: S, opportunity_id: UUID, *args: P.args, **kwargs: P.kwargs) -> R:
        with _lock:
            _active[opportunity_id] += 1
        try:
            return await function(self, opportunity_id, *args, **kwargs)
        finally:
            with _lock:
                _active[opportunity_id] -= 1
                if not _active[opportunity_id]:
                    del _active[opportunity_id]

    # Preserve keyword calls; Concatenate describes the positional protocol.
    return cast(Callable[Concatenate[S, UUID, P], Awaitable[R]], wrapped)
