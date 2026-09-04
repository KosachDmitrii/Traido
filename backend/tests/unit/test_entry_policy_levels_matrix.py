"""Parametric coverage for all five desk aggressiveness steps."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from core.enums import (
    AdmissionDecision,
    EntryDecision,
    InstrumentThesis,
    SetupType,
    TargetReachabilityClass,
)
from core.schemas import (
    EntryDecisionBundle,
    EntryQualityBreakdown,
    EntryTimingFacts,
    Quote,
    TargetPlan,
)
from trading.arrival_admission import evaluate_arrival_gate
from trading.entry_policy import set_entry_aggressiveness, thresholds_for
from trading.trade_admission import evaluate_trade_admission
from trading.trade_vetoes import is_hard_veto
from trading.zone_arrival import ArrivalType, ZoneArrivalFacts

LEVELS = (0, 25, 50, 75, 100)


@pytest.fixture(params=LEVELS, ids=[f"aggr-{l}" for l in LEVELS])
def level(request: pytest.FixtureRequest) -> int:
    value = int(request.param)
    set_entry_aggressiveness(value, actor="test")
    return value


def _quote() -> Quote:
    return Quote(
        symbol="TEST",
        bid=Decimal("100"),
        ask=Decimal("100.05"),
        ts=datetime.now(UTC),
        source="test",
    )


def _healthy_bundle(*, entry_q: int = 70, setup_q: int = 75) -> EntryDecisionBundle:
    facts = EntryTimingFacts(current_price=100.0, atr=2.0, stop_distance_atr=2.0)
    return EntryDecisionBundle(
        thesis=InstrumentThesis.BULLISH,
        entry_decision=EntryDecision.BUY_NOW,
        entry_quality=entry_q,
        setup_quality=setup_q,
        breakdown=EntryQualityBreakdown(
            price_location=entry_q,
            vwap_location=entry_q,
            atr_extension=entry_q,
            pullback_quality=entry_q,
            remaining_reward=entry_q,
            support_structure=entry_q,
            resistance_structure=entry_q,
            short_term_momentum=entry_q,
            volume_confirmation=entry_q,
            market_alignment=entry_q,
            signal_drift=entry_q,
        ),
        facts=facts,
        entry_zone_low=Decimal("98"),
        entry_zone_high=Decimal("100"),
        stop_price=Decimal("95"),
        target=TargetPlan(
            price=Decimal("110"),
            model="2R",
            reachability=TargetReachabilityClass.REALISTIC,
        ),
    )


def test_thresholds_monotonic_softening(level: int) -> None:
    th = thresholds_for(level)
    assert th.aggressiveness == level
    strong = thresholds_for(0)
    weak = thresholds_for(100)
    if level == 0:
        assert th.min_entry_quality == strong.min_entry_quality
    if level == 100:
        assert th.min_entry_quality == weak.min_entry_quality
    assert weak.min_entry_quality <= strong.min_entry_quality
    assert weak.quote_max_age_sec >= strong.quote_max_age_sec
    assert weak.max_spread_bps >= strong.max_spread_bps


def test_fast_pullback_floor_by_level(level: int) -> None:
    th = thresholds_for(level)
    arrival = ZoneArrivalFacts(
        score=28.0,
        arrival_type=ArrivalType.FAST_PULLBACK,
        arrival_speed_pct=None,
        arrival_speed_atr=None,
        atr_velocity=None,
        bars_to_zone=3,
        red_bar_ratio=0.2,
        consecutive_red_bars=1,
        largest_red_bar_atr=0.5,
        sell_volume_ratio=1.0,
        volume_acceleration=1.0,
        gap_down_pct=None,
        crash_velocity=False,
        structural_damage=False,
        reason_codes=[],
    )
    gate = evaluate_arrival_gate(arrival, th)
    if level == 100:
        assert gate.blocked is False
    elif level == 0:
        assert gate.blocked is True


def test_zone_arrival_missing_is_hard_veto() -> None:
    assert is_hard_veto("ZONE_ARRIVAL_MISSING")


def test_missing_arrival_blocks_admission(level: int) -> None:
    admission = evaluate_trade_admission(
        bundle=_healthy_bundle(),
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(),
        bars_count=40,
        last_bar_ts=datetime.now(UTC),
        require_bars=True,
        entry=100.0,
        stop=95.0,
        target=110.0,
        zone_arrival=None,
    )
    assert admission.decision is AdmissionDecision.WAIT
    assert "ZONE_ARRIVAL_MISSING" in admission.reason_codes
    assert admission.decision is not AdmissionDecision.BUY_ALLOWED


def test_breakpoints_at_50_and_75() -> None:
    th49 = thresholds_for(50)
    th74 = thresholds_for(75)
    th100 = thresholds_for(100)
    assert th49.allow_soft_chase_buy is True
    assert thresholds_for(25).allow_soft_chase_buy is False
    assert th74.require_vwap_hold is False
    assert th74.allow_sell_off_arrival is True
    assert th100.require_momentum_flip is False
    assert thresholds_for(75).require_momentum_flip is True
