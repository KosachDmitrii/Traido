"""Trade Admission — NEM/LLY regressions and gate tests."""

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
    SetupQualityBreakdown,
    TargetPlan,
)
from trading.chase_facts import HARD_CHASE_LIMIT, compute_chase_facts
from trading.data_integrity import check_data_integrity
from trading.effective_rr import compute_effective_rr
from trading.entry_policy import set_entry_aggressiveness
from trading.trade_admission import evaluate_trade_admission
from trading.zone_arrival import ArrivalType, ZoneArrivalFacts


def _bundle(
    *,
    price: float,
    setup_q: int = 85,
    entry_q: int = 75,
    zone_low: float = 111.8,
    zone_high: float = 113.2,
    entry: float = 112.5,
    stop: float = 108.0,
    target: float = 125.0,
    atr: float = 2.0,
) -> EntryDecisionBundle:
    facts = EntryTimingFacts(
        current_price=price,
        atr=atr,
        distance_from_vwap_pct=-5.0,
        distance_from_fast_ema_pct=8.0,
        nearest_support=stop,
        stop_distance_atr=max((entry - stop) / atr, 0.1) if atr else None,
    )
    return EntryDecisionBundle(
        thesis=InstrumentThesis.BULLISH,
        entry_decision=EntryDecision.BUY_NOW,
        entry_quality=entry_q,
        setup_quality=setup_q,
        setup_breakdown=SetupQualityBreakdown(
            trend_structure=setup_q,
            impulse_quality=setup_q,
            retracement_structure=setup_q,
            volume_participation=setup_q,
            support_structure=setup_q,
            market_alignment=setup_q,
            catalyst=setup_q,
            liquidity=setup_q,
        ),
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
        entry_zone_low=Decimal(str(zone_low)),
        entry_zone_high=Decimal(str(zone_high)),
        stop_price=Decimal(str(stop)),
        target=TargetPlan(
            price=Decimal(str(target)),
            model="2R",
            reachability=TargetReachabilityClass.REALISTIC,
        ),
    )


def _quote(bid: float, ask: float) -> Quote:
    return Quote(
        symbol="TEST",
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        ts=datetime.now(UTC),
        source="test",
    )


def test_nem_regression_pullback_outside_zone() -> None:
    """High setup + price far above pullback zone → WAIT, not BUY."""
    set_entry_aggressiveness(0, actor="test")
    bundle = _bundle(price=124.32, setup_q=90, entry_q=45, zone_low=111.8, zone_high=113.2)
    admission = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(124.0, 124.32),
        entry=112.5,
        stop=108.0,
        target=125.0,
    )
    assert admission.decision is AdmissionDecision.WAIT
    assert admission.admitted is False
    assert "ENTRY_OUTSIDE_ALLOWED_ZONE" in admission.vetoes or any(
        "ENTRY_OUTSIDE" in r for r in admission.reason_codes
    )


def test_deep_undercut_below_zone_is_wait_not_buy() -> None:
    """Price under the pullback zone with structural damage → NO_TRADE."""
    set_entry_aggressiveness(100, actor="test")
    bundle = _bundle(price=100.0, setup_q=80, entry_q=55, zone_low=111.8, zone_high=113.2)
    admission = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(99.9, 100.0),
        entry=112.5,
        stop=108.0,
        target=125.0,
        stop_plan_model="structure",
        stop_structural_source="entry_zone_low",
        stop_structural_level=111.8,
    )
    assert admission.decision is AdmissionDecision.NO_TRADE
    assert admission.admitted is False
    assert "ENTRY_OUTSIDE_ALLOWED_ZONE" in admission.vetoes or any(
        "ENTRY_OUTSIDE" in r for r in admission.reason_codes
    )


def test_lly_regression_insufficient_effective_rr() -> None:
    """R:R ≈ 1.66 must block BUY at default 2.0 floor."""
    set_entry_aggressiveness(0, actor="test")
    rr = compute_effective_rr(
        entry=1168.47,
        stop=1153.26,
        target=1193.77,
        quote=_quote(1168.0, 1168.47),
        slippage_bps=0.0,
    )
    assert rr.effective_rr == pytest.approx(1.66, abs=0.02)

    facts = EntryTimingFacts(current_price=1168.47, atr=8.0)
    bundle = EntryDecisionBundle(
        thesis=InstrumentThesis.BULLISH,
        entry_decision=EntryDecision.BUY_NOW,
        entry_quality=81,
        setup_quality=89,
        setup_breakdown=SetupQualityBreakdown(
            trend_structure=89,
            impulse_quality=89,
            retracement_structure=89,
            volume_participation=89,
            support_structure=89,
            market_alignment=89,
            catalyst=89,
            liquidity=89,
        ),
        breakdown=EntryQualityBreakdown(
            price_location=81,
            vwap_location=81,
            atr_extension=81,
            pullback_quality=81,
            remaining_reward=81,
            support_structure=81,
            resistance_structure=81,
            short_term_momentum=81,
            volume_confirmation=81,
            market_alignment=81,
            signal_drift=81,
        ),
        facts=facts,
        stop_price=Decimal("1153.26"),
        target=TargetPlan(
            price=Decimal("1193.77"),
            model="2R",
            reachability=TargetReachabilityClass.REALISTIC,
        ),
    )
    admission = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(1168.0, 1168.47),
        entry=1168.47,
        stop=1153.26,
        target=1193.77,
    )
    assert admission.decision is not AdmissionDecision.BUY_ALLOWED
    assert "INSUFFICIENT_EFFECTIVE_RR" in admission.vetoes


def test_excellent_setup_bad_entry_waits() -> None:
    bundle = _bundle(price=124.32, setup_q=88, entry_q=38)
    admission = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(124.0, 124.32),
    )
    assert admission.decision is AdmissionDecision.WAIT


def test_stale_data_blocks() -> None:
    old = datetime(2020, 1, 1, tzinfo=UTC)
    quote = Quote(
        symbol="X",
        bid=Decimal(100),
        ask=Decimal("100.1"),
        ts=old,
        source="test",
    )
    data = check_data_integrity(quote=quote)
    assert data.status.value == "unhealthy"
    bundle = _bundle(price=112.0, entry_q=80, setup_q=80)
    admission = evaluate_trade_admission(bundle=bundle, quote=quote)
    assert admission.decision is AdmissionDecision.DATA_BLOCKED


def test_aggressiveness_100_hard_veto_still_blocks() -> None:
    set_entry_aggressiveness(100, actor="test")
    bundle = _bundle(price=124.32, setup_q=95, entry_q=90)
    admission = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(124.0, 124.32),
    )
    assert admission.admitted is False
    assert admission.decision is not AdmissionDecision.BUY_ALLOWED


def test_all_gates_pass_buy_allowed() -> None:
    set_entry_aggressiveness(0, actor="test")
    bundle = _bundle(
        price=112.0,
        setup_q=80,
        entry_q=75,
        zone_low=111.8,
        zone_high=113.2,
        entry=112.0,
        stop=108.0,
        target=125.0,
    )
    arrival = ZoneArrivalFacts(
        score=65.0,
        arrival_type=ArrivalType.NORMAL_PULLBACK,
        arrival_speed_pct=1.0,
        arrival_speed_atr=0.5,
        atr_velocity=0.2,
        bars_to_zone=4,
        red_bar_ratio=0.3,
        consecutive_red_bars=1,
        largest_red_bar_atr=0.4,
        sell_volume_ratio=1.0,
        volume_acceleration=1.0,
        gap_down_pct=None,
        crash_velocity=False,
        structural_damage=False,
        reason_codes=[],
    )
    admission = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(111.95, 112.0),
        entry=112.0,
        stop=108.0,
        target=125.0,
        zone_arrival=arrival,
    )
    assert admission.decision is AdmissionDecision.BUY_ALLOWED
    assert admission.admitted is True


def test_extreme_chase_score_high() -> None:
    facts = EntryTimingFacts(
        current_price=130.0,
        atr=2.0,
        distance_from_vwap_pct=12.0,
        distance_from_fast_ema_pct=15.0,
        atr_extension=3.5,
        recent_impulse_atr=3.0,
        signal_to_current_drift_pct=5.0,
        remaining_expected_reward_pct=1.0,
        retracement_pct=0.10,
        impulse_grade="C",
        pullback_vol_ratio=1.5,
        pullback_index=4,
    )
    chase = compute_chase_facts(facts, zone_high=113.2)
    assert chase.score >= HARD_CHASE_LIMIT


def test_cushion_band_allows_zone_when_mark_inside_ask_in_band() -> None:
    """Trigger mark inside ±0.2 ATR must not fail ENTRY_OUTSIDE on a wider ask."""
    set_entry_aggressiveness(100, actor="test")
    zone_lo, zone_hi = 69.301, 72.295
    atr = 1.195
    mark = 72.50
    bundle = _bundle(
        price=72.52,
        setup_q=52,
        entry_q=50,
        zone_low=zone_lo,
        zone_high=zone_hi,
        entry=72.295,
        stop=67.505,
        target=81.875,
        atr=atr,
    )
    admission = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(72.48, 72.52),
        entry=72.295,
        stop=67.505,
        target=81.875,
        zone_entry_price=mark,
    )
    assert "ENTRY_OUTSIDE_ALLOWED_ZONE" not in admission.reason_codes


def test_cushion_rr_scores_at_planned_entry_not_ask() -> None:
    zone_lo, zone_hi = 69.301, 72.295
    atr = 1.195
    rr = compute_effective_rr(
        entry=72.295,
        stop=67.505,
        target=81.875,
        quote=_quote(72.48, 72.52),
        zone_low=zone_lo,
        zone_high=zone_hi,
        atr=atr,
        slippage_bps=0.0,
    )
    rr_ask = compute_effective_rr(
        entry=72.295,
        stop=67.505,
        target=81.875,
        quote=_quote(72.48, 72.52),
        slippage_bps=0.0,
    )
    assert rr.effective_entry == pytest.approx(72.295, abs=0.01)
    assert rr.effective_rr > rr_ask.effective_rr


def test_cushion_fill_suppresses_chase_and_softens_atr_stop() -> None:
    """Mark inside ±0.2 ATR must not hard-block on chase / ATR stop at revalidation."""
    set_entry_aggressiveness(100, actor="test")
    zone_lo, zone_hi = 69.301, 72.295
    atr = 1.195
    mark = 72.32
    chasey = EntryTimingFacts(
        current_price=72.55,
        atr=atr,
        distance_from_vwap_pct=12.0,
        distance_from_fast_ema_pct=15.0,
        atr_extension=3.5,
        recent_impulse_atr=3.0,
        signal_to_current_drift_pct=5.0,
        remaining_expected_reward_pct=1.0,
        stop_distance_atr=(72.295 - 67.505) / atr,
    )
    bundle = EntryDecisionBundle(
        thesis=InstrumentThesis.BULLISH,
        entry_decision=EntryDecision.BUY_NOW,
        entry_quality=50,
        setup_quality=52,
        setup_breakdown=SetupQualityBreakdown(
            trend_structure=52,
            impulse_quality=52,
            retracement_structure=52,
            volume_participation=52,
            support_structure=52,
            market_alignment=52,
            catalyst=52,
            liquidity=52,
        ),
        breakdown=EntryQualityBreakdown(
            price_location=50,
            vwap_location=50,
            atr_extension=50,
            pullback_quality=50,
            remaining_reward=50,
            support_structure=50,
            resistance_structure=50,
            short_term_momentum=50,
            volume_confirmation=50,
            market_alignment=50,
            signal_drift=50,
        ),
        facts=chasey,
        entry_zone_low=Decimal(str(zone_lo)),
        entry_zone_high=Decimal(str(zone_hi)),
        stop_price=Decimal("67.505"),
        target=TargetPlan(
            price=Decimal("81.875"),
            model="2R",
            reachability=TargetReachabilityClass.UNREALISTIC,
        ),
    )
    without = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(72.58, 72.60),
        entry=72.295,
        stop=67.505,
        target=81.875,
    )
    assert "EXTREME_CHASE" in without.reason_codes

    with_cushion = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(72.30, 72.33),
        entry=72.295,
        stop=67.505,
        target=81.875,
        target_plan=TargetPlan(
            price=Decimal("81.875"),
            model="2R",
            reachability=TargetReachabilityClass.UNREALISTIC,
        ),
        zone_entry_price=mark,
        cushion_fill=True,
    )
    assert "EXTREME_CHASE" in with_cushion.reason_codes
    assert "INVALID_STOP" not in with_cushion.vetoes
    assert "TARGET_UNREALISTIC" not in with_cushion.vetoes


@pytest.mark.parametrize("level", [0, 25, 50, 75, 100])
def test_zone_arrival_missing_blocks_all_levels(level: int) -> None:
    set_entry_aggressiveness(level, actor="test")
    bundle = _bundle(price=112.0, setup_q=80, entry_q=70)
    admission = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(112.0, 112.05),
        bars_count=40,
        last_bar_ts=datetime.now(UTC),
        require_bars=True,
        entry=112.5,
        stop=108.0,
        target=125.0,
        zone_arrival=None,
    )
    assert admission.decision is AdmissionDecision.WAIT
    assert admission.admitted is False
    assert "ZONE_ARRIVAL_MISSING" in admission.reason_codes
