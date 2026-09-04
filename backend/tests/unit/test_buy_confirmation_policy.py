"""Candidate quality stays fixed; the slider only relaxes final BUY confirms."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

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
from trading.buy_confirmation import (
    ARRIVAL_CONFIRMATION_MISSING,
    BASE_RR_FLOOR,
    BUY_READY_CANDIDATE,
    CANDIDATE_ENTRY_FLOOR,
    CANDIDATE_SETUP_FLOOR,
    EFFECTIVE_RR_TOO_LOW,
    ENTRY_CONFIRMATION_BELOW_FLOOR,
    MOMENTUM_CONFIRMATION_MISSING,
    NOT_BUY_READY,
    SETUP_CONFIRMATION_BELOW_FLOOR,
    VWAP_CONFIRMATION_MISSING,
    buy_confirmation_for,
    evaluate_buy_ready,
)
from trading.entry_policy import (
    CANDIDATE_POLICY_LEVEL,
    get_entry_thresholds,
    set_entry_aggressiveness,
    thresholds_for,
)
from trading.entry_quality import decide_entry
from trading.entry_timing import evaluate_timing, zone_from_facts
from trading.trade_admission import evaluate_trade_admission
from trading.zone_arrival import ArrivalType, ZoneArrivalFacts


def _bundle(
    *,
    price: float = 100.0,
    setup_q: int = 70,
    entry_q: int = 65,
    zone_low: float = 99.0,
    zone_high: float = 101.0,
    stop: float = 90.0,
    target: float = 120.0,
    atr: float = 2.0,
    momentum: float | None = 0.15,
    vwap_pct: float | None = -0.10,
    vol_ratio: float | None = 0.90,
    nearest_support: float | None = 90.0,
) -> EntryDecisionBundle:
    facts = EntryTimingFacts(
        current_price=price,
        atr=atr,
        distance_from_vwap_pct=vwap_pct,
        distance_from_fast_ema_pct=0.5,
        nearest_support=nearest_support,
        short_term_momentum_pct=momentum,
        pullback_vol_ratio=vol_ratio,
        stop_distance_atr=max((price - stop) / atr, 0.1) if atr else None,
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


def _quote(bid: float, ask: float, *, ts: datetime | None = None) -> Quote:
    return Quote(
        symbol="TEST",
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        ts=ts or datetime.now(UTC),
        source="test",
    )


def _admit(
    bundle: EntryDecisionBundle,
    *,
    entry: float | None = None,
    stop: float | None = None,
    target: float | None = None,
    quote: Quote | None = None,
    setup_type: SetupType = SetupType.BREAKOUT_CONTINUATION,
    regime_allowed: bool = True,
) -> object:
    price = bundle.facts.current_price
    return evaluate_trade_admission(
        bundle=bundle,
        setup_type=setup_type,
        quote=quote or _quote(price - 0.02, price),
        entry=entry if entry is not None else price,
        stop=stop if stop is not None else float(bundle.stop_price or 90),
        target=target if target is not None else float(bundle.target.price),
        stop_plan_model="structure",
        stop_structural_source="nearest_support",
        stop_structural_level=float(bundle.stop_price or 90),
        regime_allowed=regime_allowed,
    )


def test_a_weak_mode_bad_structure_is_no_trade() -> None:
    set_entry_aggressiveness(100, actor="test")
    bundle = _bundle(nearest_support=101.0, price=100.0)
    admission = _admit(bundle)
    assert admission.decision is AdmissionDecision.NO_TRADE
    assert admission.admitted is False
    assert admission.buy_ready is False


def test_b_weak_mode_below_candidate_floor_is_not_buy_ready() -> None:
    set_entry_aggressiveness(100, actor="test")
    ready = evaluate_buy_ready(
        candidate_exists=True,
        structurally_valid=True,
        price_in_entry_zone=True,
        stop_valid=True,
        target_valid=True,
        planned_rr=2.0,
        data_fresh=True,
        regime_allowed=True,
        hard_veto=False,
        setup_quality=40,
        entry_quality=50,
    )
    assert ready.ready is False
    assert "CANDIDATE_SETUP_BELOW_FLOOR" in ready.reason_codes

    admission = _admit(_bundle(setup_q=40, entry_q=50, target=125.0))
    assert admission.buy_ready is False
    assert admission.decision is not AdmissionDecision.BUY_ALLOWED
    assert NOT_BUY_READY in admission.reason_codes or "CANDIDATE_SETUP_BELOW_FLOOR" in (
        admission.reason_codes
    )


def test_c_medium_slightly_weak_momentum_may_continue() -> None:
    set_entry_aggressiveness(50, actor="test")
    bundle = _bundle(momentum=-0.01, setup_q=60, entry_q=55, target=125.0)
    admission = _admit(bundle)
    assert BUY_READY_CANDIDATE in admission.reason_codes
    assert admission.buy_ready is True
    assert MOMENTUM_CONFIRMATION_MISSING not in admission.reason_codes
    assert admission.decision in {
        AdmissionDecision.BUY_ALLOWED,
        AdmissionDecision.WAIT,
    }


def test_d_strong_unconfirmed_momentum_waits() -> None:
    set_entry_aggressiveness(0, actor="test")
    bundle = _bundle(momentum=-0.01, setup_q=60, entry_q=55, target=125.0)
    admission = _admit(bundle)
    assert admission.buy_ready is True
    assert admission.decision is AdmissionDecision.WAIT
    assert MOMENTUM_CONFIRMATION_MISSING in admission.reason_codes
    assert admission.admitted is False


def test_e_weak_missing_vwap_only_may_buy() -> None:
    set_entry_aggressiveness(100, actor="test")
    bundle = _bundle(
        setup_q=60,
        entry_q=55,
        target=125.0,
        vwap_pct=-0.80,
        momentum=0.05,
        vol_ratio=0.9,
    )
    admission = _admit(bundle)
    assert admission.buy_ready is True
    assert (
        VWAP_CONFIRMATION_MISSING
        not in [c for c in admission.reason_codes if c != "BUY_CONFIRMATION_RELAXED"]
        or admission.decision is AdmissionDecision.BUY_ALLOWED
    )
    assert admission.decision is AdmissionDecision.BUY_ALLOWED
    assert admission.admitted is True


def test_f_weak_stale_data_is_data_blocked() -> None:
    set_entry_aggressiveness(100, actor="test")
    stale = _quote(99.98, 100.0, ts=datetime(2020, 1, 1, tzinfo=UTC))
    admission = _admit(_bundle(), quote=stale)
    assert admission.decision is AdmissionDecision.DATA_BLOCKED
    assert admission.buy_ready is False
    assert "DATA_BLOCKED" in admission.reason_codes


def test_g_weak_invalid_stop_is_hard_veto() -> None:
    set_entry_aggressiveness(100, actor="test")
    admission = _admit(_bundle(), stop=100.0, target=125.0)
    assert admission.decision is AdmissionDecision.NO_TRADE
    assert admission.admitted is False
    assert admission.buy_ready is False


def test_h_weak_setup_deficit_four_forbids_compensation() -> None:
    set_entry_aggressiveness(100, actor="test")
    admission = _admit(_bundle(setup_q=51, entry_q=55, target=125.0))
    assert "SETUP_COMPENSATED" not in admission.reason_codes
    assert admission.buy_ready is False
    assert admission.decision is not AdmissionDecision.BUY_ALLOWED


def test_i_weak_setup_deficit_two_allows_confirmation_relaxation() -> None:
    set_entry_aggressiveness(100, actor="test")
    admission = _admit(_bundle(setup_q=53, entry_q=55, target=125.0, momentum=0.05))
    assert admission.buy_ready is True
    assert SETUP_CONFIRMATION_BELOW_FLOOR not in admission.reason_codes
    assert ENTRY_CONFIRMATION_BELOW_FLOOR not in admission.reason_codes
    assert admission.decision is AdmissionDecision.BUY_ALLOWED
    assert admission.confirmation_relaxed is True


def test_j_slider_does_not_change_candidate_or_wait_funnel() -> None:
    from agents.trader.policy import trader_gates_for
    from tests.unit.test_entry_timing_f3 import _snap

    cand_fields = (
        "require_uptrend",
        "allow_range",
        "require_ema_stack",
        "rsi_overbought",
        "chase_ext_frac",
        "near_sma_frac",
        "allow_below_sma",
        "zone_gap_frac",
        "zone_min_width_atr",
        "zone_max_width_atr",
        "zone_require_reclaim",
        "zone_max_touch_count",
        "zone_invalidate_below_atr",
        "quote_max_age_sec",
        "min_setup_quality",
        "min_entry_quality",
        "wait_ttl_minutes",
        "max_spread_bps",
    )
    strong = thresholds_for(0)
    weak = thresholds_for(100)
    medium = thresholds_for(CANDIDATE_POLICY_LEVEL)
    for field in cand_fields:
        assert getattr(strong, field) == getattr(medium, field) == getattr(weak, field), field

    assert strong.min_setup_quality == CANDIDATE_SETUP_FLOOR
    assert strong.min_entry_quality == CANDIDATE_ENTRY_FLOOR
    assert buy_confirmation_for(0).min_effective_rr == 2.0
    assert buy_confirmation_for(100).min_effective_rr == BASE_RR_FLOOR

    facts = evaluate_timing(_snap(close=110.0, sma20=100.0, atr=2.0, vwap=100.0))
    set_entry_aggressiveness(0, actor="test")
    zone0 = zone_from_facts(facts)
    dec0 = decide_entry(InstrumentThesis.BULLISH, facts, technical_score=80)
    gates0 = trader_gates_for()
    set_entry_aggressiveness(100, actor="test")
    zone100 = zone_from_facts(facts)
    dec100 = decide_entry(InstrumentThesis.BULLISH, facts, technical_score=80)
    gates100 = trader_gates_for()
    assert zone0 == zone100
    assert dec0.entry_decision is dec100.entry_decision
    assert gates0.require_uptrend is gates100.require_uptrend
    assert gates0.rsi_overbought == gates100.rsi_overbought
    assert gates0.chase_ext_frac == gates100.chase_ext_frac


def test_candidate_floors_are_fixed_across_slider() -> None:
    for level in (0, 25, 50, 75, 100):
        set_entry_aggressiveness(level, actor="test")
        th = get_entry_thresholds()
        assert th.min_setup_quality == CANDIDATE_SETUP_FLOOR
        assert th.min_entry_quality == CANDIDATE_ENTRY_FLOOR
        assert th.buy_confirmation_strictness == level
        assert th.quote_max_age_sec == 30.0
        assert th.structural_arrival_hard is True


def _path_arrival(
    *,
    score: float = 32.0,
    arrival_type: ArrivalType = ArrivalType.UNKNOWN,
) -> ZoneArrivalFacts:
    return ZoneArrivalFacts(
        score=score,
        arrival_type=arrival_type,
        arrival_speed_pct=None,
        arrival_speed_atr=None,
        atr_velocity=None,
        bars_to_zone=None,
        red_bar_ratio=0.3,
        consecutive_red_bars=1,
        largest_red_bar_atr=0.4,
        sell_volume_ratio=1.0,
        volume_acceleration=1.0,
        gap_down_pct=None,
        crash_velocity=False,
        structural_damage=False,
        reason_codes=["NO_PULLBACK_PATH"],
    )


def test_out_of_zone_support_undercut_is_wait_not_no_trade() -> None:
    """Price below nearest support but still outside the zone is reclaim WAIT."""
    decisions: list[AdmissionDecision] = []
    for level in (0, 50, 100):
        set_entry_aggressiveness(level, actor="test")
        bundle = _bundle(
            price=98.0,
            zone_low=99.0,
            zone_high=101.0,
            setup_q=65,
            entry_q=70,
            target=125.0,
            nearest_support=99.5,
        )
        admission = evaluate_trade_admission(
            bundle=bundle,
            setup_type=SetupType.PULLBACK_CONTINUATION,
            quote=_quote(97.98, 98.0),
            entry=100.0,
            stop=90.0,
            target=125.0,
            stop_plan_model="structure",
            stop_structural_source="nearest_support",
            stop_structural_level=90.0,
            zone_arrival=_path_arrival(score=32.0),
        )
        decisions.append(admission.decision)
        assert admission.decision is AdmissionDecision.WAIT, level
        assert admission.admitted is False
        assert "STRUCTURAL_DAMAGE" not in admission.vetoes
        assert "STRUCTURAL_DAMAGE" not in admission.reason_codes
    assert len(set(decisions)) == 1


def test_in_zone_support_break_stays_no_trade() -> None:
    set_entry_aggressiveness(100, actor="test")
    bundle = _bundle(
        price=100.0,
        zone_low=99.0,
        zone_high=101.0,
        nearest_support=101.0,
        target=125.0,
    )
    admission = _admit(bundle, setup_type=SetupType.PULLBACK_CONTINUATION)
    assert admission.decision is AdmissionDecision.NO_TRADE
    assert "STRUCTURAL_DAMAGE" in admission.vetoes or "STRUCTURAL_DAMAGE" in admission.reason_codes


def test_out_of_zone_low_arrival_is_wait_at_every_slider() -> None:
    """Score 32 is the not-yet-arrived path. It must not decide WAIT vs NO_TRADE."""
    decisions: list[AdmissionDecision] = []
    for level in (0, 50, 100):
        set_entry_aggressiveness(level, actor="test")
        bundle = _bundle(
            price=102.0,
            zone_low=99.0,
            zone_high=101.0,
            setup_q=60,
            entry_q=58,
            target=125.0,
            nearest_support=90.0,
        )
        admission = evaluate_trade_admission(
            bundle=bundle,
            setup_type=SetupType.PULLBACK_CONTINUATION,
            quote=_quote(101.98, 102.0),
            entry=100.0,
            stop=90.0,
            target=125.0,
            stop_plan_model="structure",
            stop_structural_source="nearest_support",
            stop_structural_level=90.0,
            zone_arrival=_path_arrival(score=32.0),
        )
        decisions.append(admission.decision)
        assert admission.decision is AdmissionDecision.WAIT
        assert admission.buy_ready is False
        assert admission.admitted is False
        assert not any(c.startswith("ZONE_ARRIVAL_QUALITY_LOW") for c in admission.reason_codes)
        assert ARRIVAL_CONFIRMATION_MISSING not in admission.reason_codes
    assert len(set(decisions)) == 1


def test_in_zone_arrival_quality_follows_slider_only() -> None:
    arrival = _path_arrival(score=28.0, arrival_type=ArrivalType.FAST_PULLBACK)
    bundle = _bundle(
        price=100.0,
        zone_low=99.0,
        zone_high=101.0,
        setup_q=70,
        entry_q=65,
        target=125.0,
        momentum=0.15,
        vwap_pct=-0.10,
        vol_ratio=0.90,
        nearest_support=90.0,
    )
    set_entry_aggressiveness(0, actor="test")
    strong = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(99.98, 100.0),
        entry=100.0,
        stop=90.0,
        target=125.0,
        stop_plan_model="structure",
        stop_structural_source="nearest_support",
        stop_structural_level=90.0,
        zone_arrival=arrival,
    )
    assert strong.buy_ready is True
    assert strong.decision is AdmissionDecision.WAIT
    assert ARRIVAL_CONFIRMATION_MISSING in strong.reason_codes

    set_entry_aggressiveness(100, actor="test")
    weak = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(99.98, 100.0),
        entry=100.0,
        stop=90.0,
        target=125.0,
        stop_plan_model="structure",
        stop_structural_source="nearest_support",
        stop_structural_level=90.0,
        zone_arrival=arrival,
    )
    assert weak.buy_ready is True
    assert weak.decision is AdmissionDecision.BUY_ALLOWED
    assert ARRIVAL_CONFIRMATION_MISSING not in weak.reason_codes


def test_effective_rr_confirmation_rejects_at_strong_not_as_hard_veto() -> None:
    set_entry_aggressiveness(0, actor="test")
    # planned RR = (116.6-100)/(100-90) = 1.66; Strong wants 2.0
    bundle = _bundle(price=100.0, stop=90.0, target=116.6, setup_q=80, entry_q=75)
    admission = _admit(bundle, stop=90.0, target=116.6)
    assert admission.buy_ready is True
    assert admission.decision is AdmissionDecision.WAIT
    assert EFFECTIVE_RR_TOO_LOW in admission.reason_codes
    assert admission.admitted is False
