"""Transient WAIT conditions must not demote TRIGGERED watches."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from core.enums import EntryWatchStatus, InstrumentThesis, SetupType
from core.schemas import EntryTimingFacts, EntryWatch, Quote
from trading.entry_watches import SPREAD_ACCEPTABLE
from trading.wait_conditions import TRANSIENT_TRIGGER_CONDITIONS, unmet_wait_conditions


def _watch() -> EntryWatch:
    now = datetime.now(UTC)
    return EntryWatch(
        id=uuid4(),
        symbol="FCX",
        strategy_version="test",
        status=EntryWatchStatus.TRIGGERED,
        thesis=InstrumentThesis.BULLISH,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        signal_price=Decimal("70"),
        current_price_at_creation=Decimal("72.5"),
        entry_zone_low=Decimal("69.3"),
        entry_zone_high=Decimal("72.3"),
        planned_entry=Decimal("72.3"),
        planned_stop=Decimal("67.5"),
        planned_target=Decimal("81.9"),
        entry_quality_at_creation=55,
        required_conditions=[SPREAD_ACCEPTABLE],
        valid_until=now + timedelta(hours=2),
        created_at=now,
        reasons=[],
        last_price=Decimal("72.5"),
    )


def test_spread_acceptable_is_transient() -> None:
    assert SPREAD_ACCEPTABLE in TRANSIENT_TRIGGER_CONDITIONS


def test_wide_spread_pending_is_transient_only(monkeypatch) -> None:
    monkeypatch.setattr("trading.entry_policy._cached", 100)
    watch = _watch()
    quote = Quote(
        symbol="FCX",
        bid=Decimal("72.00"),
        ask=Decimal("73.10"),
        ts=datetime.now(UTC),
        source="test",
    )
    facts = EntryTimingFacts(current_price=72.5, atr=1.5)
    pending = unmet_wait_conditions(watch, facts, quote=quote)
    assert pending == [SPREAD_ACCEPTABLE]
    assert set(pending).issubset(TRANSIENT_TRIGGER_CONDITIONS)
