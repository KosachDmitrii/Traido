"""Desk revalidation hint must reflect the newest block, not stale spread."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from core.enums import EntryWatchStatus, InstrumentThesis
from core.schemas import AdmissionSnapshot, EntryWatch
from trading.watch_desk import desk_revalidation_hint


def _watch(*, reasons: list[str]) -> EntryWatch:
    now = datetime.now(UTC)
    return EntryWatch(
        id=uuid4(),
        symbol="FCX",
        strategy_version="test",
        created_at=now,
        valid_until=now + timedelta(hours=1),
        thesis=InstrumentThesis.BULLISH,
        signal_price=Decimal("72"),
        current_price_at_creation=Decimal("72.3"),
        last_price=Decimal("72.3"),
        entry_zone_low=Decimal("69.3"),
        entry_zone_high=Decimal("72.3"),
        planned_entry=Decimal("72.3"),
        planned_stop=Decimal("67.5"),
        planned_target=Decimal("81.9"),
        entry_quality_at_creation=65,
        status=EntryWatchStatus.TRIGGERED,
        reasons=reasons,
    )


def test_newest_admission_bundle_beats_stale_spread() -> None:
    watch = _watch(
        reasons=[
            "TRIGGERED_CONDITIONS_PENDING:SPREAD_ACCEPTABLE",
            "EXTREME_CHASE,ATR_ONLY_STOP,INVALID_STOP,TARGET_UNREALISTIC",
        ]
    )
    watch = watch.model_copy(
        update={
            "last_price": Decimal("73.5"),
            "entry_zone_high": Decimal("72.295"),
            "admission_snapshot": AdmissionSnapshot(
                price_at_creation=73.5,
                atr_at_creation=1.195,
            ),
        }
    )
    assert desk_revalidation_hint(watch) == (
        "EXTREME_CHASE,ATR_ONLY_STOP,INVALID_STOP,TARGET_UNREALISTIC"
    )


def test_spread_only_when_latest() -> None:
    watch = _watch(reasons=["TRIGGERED_CONDITIONS_PENDING:SPREAD_ACCEPTABLE"])
    watch = watch.model_copy(
        update={
            "last_price": Decimal("73.5"),
            "entry_zone_high": Decimal("72.295"),
            "admission_snapshot": AdmissionSnapshot(
                price_at_creation=73.5,
                atr_at_creation=1.195,
            ),
        }
    )
    assert desk_revalidation_hint(watch) == "SPREAD_ACCEPTABLE"


def test_spread_shown_inside_cushion_band() -> None:
    watch = _watch(reasons=["TRIGGERED_CONDITIONS_PENDING:SPREAD_ACCEPTABLE"])
    watch = watch.model_copy(
        update={
            "last_price": Decimal("72.32"),
            "entry_zone_low": Decimal("69.301"),
            "entry_zone_high": Decimal("72.295"),
            "admission_snapshot": AdmissionSnapshot(
                price_at_creation=72.32,
                atr_at_creation=1.195,
            ),
        }
    )
    assert desk_revalidation_hint(watch) == "SPREAD_ACCEPTABLE"


def test_cushion_suppresses_stale_chase_hint() -> None:
    watch = _watch(
        reasons=[
            "EXTREME_CHASE,ATR_ONLY_STOP,INVALID_STOP,TARGET_UNREALISTIC",
        ]
    )
    watch = watch.model_copy(
        update={
            "last_price": Decimal("72.32"),
            "entry_zone_low": Decimal("69.301"),
            "entry_zone_high": Decimal("72.295"),
            "admission_snapshot": AdmissionSnapshot(
                price_at_creation=72.32,
                atr_at_creation=1.195,
            ),
        }
    )
    assert desk_revalidation_hint(watch) is None


def test_strip_resolved_spread_hints() -> None:
    from trading.watch_desk import strip_resolved_spread_hints

    assert strip_resolved_spread_hints("SPREAD_ACCEPTABLE", spread_acceptable=True) is None
    assert (
        strip_resolved_spread_hints(
            "SPREAD_ACCEPTABLE,EXTREME_CHASE",
            spread_acceptable=True,
        )
        == "EXTREME_CHASE"
    )
    assert (
        strip_resolved_spread_hints("SPREAD_ACCEPTABLE", spread_acceptable=False)
        == "SPREAD_ACCEPTABLE"
    )
