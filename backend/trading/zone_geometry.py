"""Shared zone math — entry corridor, reclaim, invalidation, touch counting."""

from __future__ import annotations

import threading
from uuid import UUID

from core.schemas import EntryWatch
from trading.entry_policy import EntryThresholds, get_entry_thresholds
from trading.entry_watches import price_in_zone

_TOUCH_LOCK = threading.Lock()
_zone_touch_counts: dict[UUID, int] = {}


def zone_mid(watch: EntryWatch) -> float:
    lo = float(watch.entry_zone_low)
    hi = float(watch.entry_zone_high)
    return (lo + hi) / 2.0


def zone_reclaim_met(watch: EntryWatch, price: float, th: EntryThresholds | None = None) -> bool:
    """Upper-half / mid reclaim — price in zone is not enough on strong steps."""
    th = th or get_entry_thresholds()
    if not th.zone_require_reclaim:
        return price_in_zone(price, watch)
    if not price_in_zone(price, watch):
        return False
    return price >= zone_mid(watch)


def structure_lost_below_zone(
    watch: EntryWatch,
    price: float,
    atr: float | None,
    th: EntryThresholds | None = None,
) -> bool:
    """Price broke materially below the frozen zone — thesis invalidated."""
    th = th or get_entry_thresholds()
    px = float(watch.current_price_at_creation) if watch.current_price_at_creation else price
    atr_v = atr if atr and atr > 0 else px * 0.01
    floor = float(watch.entry_zone_low) - th.zone_invalidate_below_atr * atr_v
    return price < floor


def record_zone_touch(watch_id: UUID) -> int:
    with _TOUCH_LOCK:
        n = _zone_touch_counts.get(watch_id, 0) + 1
        _zone_touch_counts[watch_id] = n
        return n


def zone_touch_count(watch_id: UUID) -> int:
    with _TOUCH_LOCK:
        return _zone_touch_counts.get(watch_id, 0)


def reset_zone_touch(watch_id: UUID) -> None:
    with _TOUCH_LOCK:
        _zone_touch_counts.pop(watch_id, None)


def zone_touch_exhausted(watch_id: UUID, th: EntryThresholds | None = None) -> bool:
    th = th or get_entry_thresholds()
    return zone_touch_count(watch_id) >= th.zone_max_touch_count
