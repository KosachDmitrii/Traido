"""Entry likelihood classification tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from core.enums import InstrumentThesis, SetupType
from core.schemas import AdmissionSnapshot, EntryTimingFacts, EntryWatch
from trading.entry_likelihood import LikelihoodClass, evaluate_entry_likelihood


def _watch(*, zone_lo: float = 111.8, zone_hi: float = 113.2) -> EntryWatch:
    now = datetime.now(UTC)
    return EntryWatch(
        id=uuid4(),
        symbol="NEM",
        strategy_version="test",
        created_at=now,
        valid_until=now + timedelta(hours=3),
        thesis=InstrumentThesis.BULLISH,
        signal_price=Decimal(120),
        current_price_at_creation=Decimal(123),
        entry_zone_low=Decimal(str(zone_lo)),
        entry_zone_high=Decimal(str(zone_hi)),
        planned_entry=Decimal("112.5"),
        planned_stop=Decimal(108),
        planned_target=Decimal(125),
        entry_quality_at_creation=45,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        admission_snapshot=AdmissionSnapshot(
            price_at_creation=123.0,
            atr_at_creation=2.0,
            setup_type=SetupType.PULLBACK_CONTINUATION,
            entry_zone_low=zone_lo,
            entry_zone_high=zone_hi,
        ),
    )


def test_close_to_zone_is_high() -> None:
    watch = _watch()
    lik = evaluate_entry_likelihood(
        watch,
        price=113.0,
        facts=EntryTimingFacts(current_price=113.0, atr=2.0),
    )
    assert lik.classification is LikelihoodClass.HIGH
    assert lik.distance_atr is not None and lik.distance_atr <= 0.5


def test_far_from_zone_with_short_ttl_is_low() -> None:
    watch = _watch()
    now = datetime.now(UTC)
    watch = watch.model_copy(update={"valid_until": now + timedelta(minutes=20)})
    lik = evaluate_entry_likelihood(
        watch,
        price=124.32,
        facts=EntryTimingFacts(
            current_price=124.32,
            atr=2.0,
            distance_from_fast_ema_pct=8.0,
            short_term_momentum_pct=2.5,
        ),
        now=now,
    )
    assert lik.classification is LikelihoodClass.LOW


def test_moderate_distance() -> None:
    watch = _watch()
    lik = evaluate_entry_likelihood(
        watch,
        price=115.5,
        facts=EntryTimingFacts(current_price=115.5, atr=2.0),
    )
    assert lik.classification in {LikelihoodClass.MODERATE, LikelihoodClass.HIGH, LikelihoodClass.LOW}
