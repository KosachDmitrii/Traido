"""Zone geometry — reclaim, invalidation, touch counting."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from core.enums import EntryWatchStatus, InstrumentThesis, SetupType, Timeframe
from core.schemas import EntryWatch
from trading.entry_policy import get_entry_thresholds, set_entry_aggressiveness
from trading.zone_geometry import (
    record_zone_touch,
    reset_zone_touch,
    structure_lost_below_zone,
    zone_mid,
    zone_reclaim_met,
    zone_touch_exhausted,
)


def _watch(*, lo: float = 99.0, hi: float = 101.0) -> EntryWatch:
    now = datetime.now(UTC)
    return EntryWatch(
        id=uuid4(),
        symbol="TEST",
        strategy_version="test",
        status=EntryWatchStatus.WAITING,
        thesis=InstrumentThesis.BULLISH,
        created_at=now,
        valid_until=datetime(2099, 1, 1, tzinfo=UTC),
        signal_price=Decimal("100"),
        current_price_at_creation=Decimal("100"),
        entry_zone_low=Decimal(str(lo)),
        entry_zone_high=Decimal(str(hi)),
        planned_entry=Decimal("100"),
        planned_stop=Decimal("98"),
        planned_target=Decimal("104"),
        entry_quality_at_creation=70,
        setup_type=SetupType.PULLBACK_CONTINUATION,
    )


def test_zone_reclaim_requires_mid_on_strong_step() -> None:
    set_entry_aggressiveness(0, actor="test")
    watch = _watch(lo=99.0, hi=101.0)
    th = get_entry_thresholds()
    assert th.zone_require_reclaim is True
    assert not zone_reclaim_met(watch, 99.2, th)
    assert zone_reclaim_met(watch, zone_mid(watch), th)


def test_zone_reclaim_off_on_weak_step() -> None:
    set_entry_aggressiveness(100, actor="test")
    watch = _watch(lo=99.0, hi=101.0)
    th = get_entry_thresholds()
    assert th.zone_require_reclaim is False
    assert zone_reclaim_met(watch, 99.1, th)


def test_structure_lost_below_zone() -> None:
    set_entry_aggressiveness(50, actor="test")
    watch = _watch(lo=99.0, hi=101.0)
    th = get_entry_thresholds()
    floor = float(watch.entry_zone_low) - th.zone_invalidate_below_atr * 1.0
    assert not structure_lost_below_zone(watch, floor + 0.01, 1.0, th)
    assert structure_lost_below_zone(watch, floor - 0.01, 1.0, th)


def test_touch_count_exhaustion() -> None:
    set_entry_aggressiveness(0, actor="test")
    th = get_entry_thresholds()
    watch_id = uuid4()
    for _ in range(th.zone_max_touch_count - 1):
        record_zone_touch(watch_id)
        assert not zone_touch_exhausted(watch_id, th)
    record_zone_touch(watch_id)
    assert zone_touch_exhausted(watch_id, th)
    reset_zone_touch(watch_id)
    assert not zone_touch_exhausted(watch_id, th)
