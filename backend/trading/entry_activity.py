"""Process-local entry ownership; persisted intents own restart recovery."""

from collections import Counter
from functools import wraps
from threading import RLock

_lock = RLock()
_active = Counter()


def entry_active(opportunity_id):
    with _lock:
        return _active[opportunity_id] > 0


def track_entry(function):
    @wraps(function)
    async def wrapped(self, opportunity_id, *args, **kwargs):
        with _lock:
            _active[opportunity_id] += 1
        try:
            return await function(self, opportunity_id, *args, **kwargs)
        finally:
            with _lock:
                _active[opportunity_id] -= 1
                if not _active[opportunity_id]:
                    del _active[opportunity_id]

    return wrapped
