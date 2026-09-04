"""Adaptive entry-watch loop cadence."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from core.enums import EntryWatchStatus, InstrumentThesis
from core.schemas import EntryWatch
from trading.watch_desk import (
    WATCH_POLL_COLD_SEC,
    WATCH_POLL_HOT_SEC,
    WATCH_POLL_NEAR_SEC,
    watch_loop_interval_sec,
    watch_poll_hot,
    watch_poll_near,
)


def _watch(
    *,
    status: EntryWatchStatus = EntryWatchStatus.WAITING,
    price: float = 100.0,
    lo: float = 99.0,
    hi: float = 101.0,
    enrichment: dict | None = None,
) -> EntryWatch:
    now = datetime.now(UTC)
    return EntryWatch(
        id=uuid4(),
        symbol="TEST",
        strategy_version="test",
        created_at=now,
        valid_until=now + timedelta(hours=1),
        thesis=InstrumentThesis.BULLISH,
        signal_price=Decimal(str(price)),
        current_price_at_creation=Decimal(str(price)),
        last_price=Decimal(str(price)),
        entry_zone_low=Decimal(str(lo)),
        entry_zone_high=Decimal(str(hi)),
        planned_entry=Decimal(str(price)),
        planned_stop=Decimal("95"),
        planned_target=Decimal("110"),
        entry_quality_at_creation=70,
        status=status,
        desk_enrichment=enrichment if enrichment is not None else {},
    )


def test_triggered_is_hot() -> None:
    w = _watch(status=EntryWatchStatus.TRIGGERED)
    assert watch_poll_hot(w) is True
    assert watch_poll_near(w) is False


def test_in_zone_waiting_is_hot() -> None:
    w = _watch(price=100.0, lo=99.0, hi=101.0, enrichment={"ui_state": "IN_ZONE"})
    assert watch_poll_hot(w) is True


def test_approaching_is_near_not_hot() -> None:
    w = _watch(
        price=105.0,
        lo=99.0,
        hi=101.0,
        enrichment={"ui_state": "WAITING", "distance_to_zone_atr": 0.4},
    )
    assert watch_poll_hot(w) is False
    assert watch_poll_near(w) is True


def test_far_waiting_is_cold() -> None:
    w = _watch(
        price=120.0,
        lo=99.0,
        hi=101.0,
        enrichment={"ui_state": "WAITING", "distance_to_zone_atr": 2.5},
    )
    assert watch_poll_hot(w) is False
    assert watch_poll_near(w) is False


def test_loop_interval_picks_most_urgent() -> None:
    hot = _watch(status=EntryWatchStatus.TRIGGERED)
    cold = _watch(price=120.0, lo=99.0, hi=101.0, enrichment={"distance_to_zone_atr": 3.0})
    near = _watch(
        price=102.5,
        lo=99.0,
        hi=101.0,
        enrichment={"ui_state": "APPROACHING", "distance_to_zone_atr": 0.4},
    )
    assert watch_loop_interval_sec([]) == WATCH_POLL_COLD_SEC
    assert watch_loop_interval_sec([cold]) == WATCH_POLL_COLD_SEC
    assert watch_loop_interval_sec([cold, near]) == WATCH_POLL_NEAR_SEC
    assert watch_loop_interval_sec([cold, near, hot]) == WATCH_POLL_HOT_SEC
