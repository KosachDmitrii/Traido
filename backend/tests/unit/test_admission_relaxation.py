"""Controlled paper admission relaxation — floors, compensation, LIVE isolation."""

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
from trading.admission_relaxation import (
    COMPENSATION_MIN_RR,
    MAX_SETUP_DEFICIT,
    PAPER_QUALITY_FLOORS,
    evaluate_setup_compensation,
    paper_quality_floors,
)
from trading.effective_rr import planned_long_rr
from trading.entry_policy import set_entry_aggressiveness, thresholds_for
from trading.trade_admission import evaluate_trade_admission


def _comp(**overrides: object) -> object:
    kwargs: dict[str, object] = {
        "paper": True,
        "setup_score": 52,
        "setup_floor": 53,
        "entry_score": 50,
        "entry_floor": 48,
        "price_in_entry_zone": True,
        "rr": 2.1,
        "regime_allowed": True,
        "required_market_data_fresh": True,
        "hard_risk_block": False,
        "broker_or_data_block": False,
    }
    kwargs.update(overrides)
    return evaluate_setup_compensation(**kwargs)  # type: ignore[arg-type]


def _bundle(
    *,
    price: float,
    setup_q: int,
    entry_q: int,
    zone_low: float,
    zone_high: float,
    entry: float,
    stop: float,
    target: float,
    atr: float = 2.0,
) -> EntryDecisionBundle:
    facts = EntryTimingFacts(
        current_price=price,
        atr=atr,
        distance_from_vwap_pct=-1.0,
        distance_from_fast_ema_pct=0.5,
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


def _quote(bid: float, ask: float, *, ts: datetime | None = None) -> Quote:
    return Quote(
        symbol="TSLA",
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        ts=ts or datetime.now(UTC),
        source="test",
    )


def _admit(
    *,
    setup_q: int,
    entry_q: int,
    price: float,
    zone_low: float,
    zone_high: float,
    entry: float,
    stop: float,
    target: float,
    quote: Quote | None = None,
    setup_type: SetupType = SetupType.BREAKOUT_CONTINUATION,
    regime_allowed: bool = True,
    atr: float = 2.0,
) -> object:
    bundle = _bundle(
        price=price,
        setup_q=setup_q,
        entry_q=entry_q,
        zone_low=zone_low,
        zone_high=zone_high,
        entry=entry,
        stop=stop,
        target=target,
        atr=atr,
    )
    return evaluate_trade_admission(
        bundle=bundle,
        setup_type=setup_type,
        quote=quote or _quote(price - 0.02, price),
        entry=entry,
        stop=stop,
        target=target,
        stop_plan_model="structure",
        stop_structural_source="nearest_support",
        stop_structural_level=stop,
        regime_allowed=regime_allowed,
    )


def test_paper_floors_are_canonical() -> None:
    from trading.entry_policy import get_entry_thresholds

    assert MAX_SETUP_DEFICIT == 3
    assert COMPENSATION_MIN_RR == 2.0
    expected = {
        0: (58, 53, 2.30),
        25: (56, 51, 2.10),
        50: (53, 48, 1.90),
        75: (50, 46, 1.70),
        100: (47, 44, 1.45),
    }
    assert set(PAPER_QUALITY_FLOORS) == set(expected)
    for level, (setup, entry, weak_rr) in expected.items():
        floors = paper_quality_floors(level)
        assert floors.setup_floor == setup
        assert floors.entry_floor == entry
        assert floors.weak_setup_min_rr == weak_rr
    set_entry_aggressiveness(50, actor="test")
    paper_th = get_entry_thresholds()
    assert paper_th.min_setup_quality == 53
    assert paper_th.min_entry_quality == 48
    assert paper_th.weak_setup_min_rr == 1.90


def test_live_thresholds_for_stay_on_historical_floors() -> None:
    live = thresholds_for(50)
    paper = paper_quality_floors(50)
    assert live.min_setup_quality == 55
    assert live.min_entry_quality == 50
    assert live.weak_setup_min_rr == 2.1
    assert paper.setup_floor == 53


def test_a_setup_at_floor_uses_ordinary_pipeline() -> None:
    """A: setup=53 floor=53, entry >= floor, RR=1.6 → no compensation."""
    result = _comp(setup_score=53, setup_floor=53, rr=1.6)
    assert result.applied is False
    assert result.eligible is False
    assert result.setup_deficit == 0

    set_entry_aggressiveness(50, actor="test")
    admission = _admit(
        setup_q=53,
        entry_q=50,
        price=100.0,
        zone_low=99.0,
        zone_high=101.0,
        entry=100.0,
        stop=90.0,
        target=116.0,
    )
    assert "SETUP_COMPENSATED" not in admission.reason_codes
    assert admission.decision is not AdmissionDecision.BUY_ALLOWED


def test_b_small_deficit_compensates_and_continues() -> None:
    """B: setup=52 floor=53, in zone, RR=2.1 → SETUP_COMPENSATED, pipeline continues."""
    result = _comp(setup_score=52, setup_floor=53, rr=2.1)
    assert result.eligible is True
    assert result.applied is True
    assert result.setup_deficit == 1

    set_entry_aggressiveness(50, actor="test")
    admission = _admit(
        setup_q=52,
        entry_q=50,
        price=100.0,
        zone_low=99.0,
        zone_high=101.0,
        entry=100.0,
        stop=90.0,
        target=121.0,
    )
    assert "SETUP_COMPENSATED" in admission.reason_codes
    assert "SETUP_BELOW_FLOOR" not in admission.reason_codes
    assert admission.decision in {
        AdmissionDecision.BUY_ALLOWED,
        AdmissionDecision.WAIT,
        AdmissionDecision.NO_TRADE,
        AdmissionDecision.DATA_BLOCKED,
    }
    if admission.decision is AdmissionDecision.BUY_ALLOWED:
        assert "BUY_ALLOWED" in admission.reason_codes
        assert admission.admitted is True


def test_c_deficit_over_three_is_refused() -> None:
    """C: setup=49 floor=53, deficit=4 → no compensation."""
    result = _comp(setup_score=49, setup_floor=53, rr=2.2)
    assert result.applied is False
    assert result.setup_deficit == 4

    set_entry_aggressiveness(50, actor="test")
    admission = _admit(
        setup_q=49,
        entry_q=50,
        price=100.0,
        zone_low=99.0,
        zone_high=101.0,
        entry=100.0,
        stop=90.0,
        target=122.0,
    )
    assert "SETUP_COMPENSATED" not in admission.reason_codes
    assert "SETUP_BELOW_FLOOR" in admission.reason_codes
    assert admission.decision is AdmissionDecision.WAIT


def test_d_weak_entry_blocks_compensation() -> None:
    """D: setup below floor AND entry below floor → no compensation."""
    result = _comp(setup_score=52, entry_score=40, entry_floor=48, rr=2.5)
    assert result.applied is False
    assert result.deny_reason == "ENTRY_BELOW_FLOOR"

    set_entry_aggressiveness(50, actor="test")
    admission = _admit(
        setup_q=52,
        entry_q=40,
        price=100.0,
        zone_low=99.0,
        zone_high=101.0,
        entry=100.0,
        stop=90.0,
        target=125.0,
    )
    assert "SETUP_COMPENSATED" not in admission.reason_codes
    assert "SETUP_BELOW_FLOOR" in admission.reason_codes
    assert "ENTRY_BELOW_FLOOR" in admission.reason_codes
    assert admission.decision is AdmissionDecision.WAIT


def test_e_rr_below_compensation_floor() -> None:
    """E: setup=52, entry ok, RR=1.8 → compensation forbidden."""
    result = _comp(setup_score=52, rr=1.8)
    assert result.applied is False
    assert result.deny_reason == "RR_BELOW_COMPENSATION_FLOOR"

    set_entry_aggressiveness(50, actor="test")
    admission = _admit(
        setup_q=52,
        entry_q=50,
        price=100.0,
        zone_low=99.0,
        zone_high=101.0,
        entry=100.0,
        stop=90.0,
        target=118.0,
    )
    assert planned_long_rr(100.0, 90.0, 118.0) == 1.8
    assert "SETUP_COMPENSATED" not in admission.reason_codes


def test_f_outside_zone_blocks_compensation() -> None:
    """F: setup=52, RR=2.2, in_zone=false → no compensation."""
    result = _comp(rr=2.2, price_in_entry_zone=False)
    assert result.applied is False

    set_entry_aggressiveness(50, actor="test")
    admission = _admit(
        setup_q=52,
        entry_q=50,
        price=98.4,
        zone_low=99.0,
        zone_high=101.0,
        entry=100.0,
        stop=90.0,
        target=122.0,
    )
    assert "SETUP_COMPENSATED" not in admission.reason_codes
    assert "SETUP_BELOW_FLOOR" in admission.reason_codes


def test_g_stale_data_is_data_blocked() -> None:
    """G: otherwise compensatable candidate with stale data → DATA_BLOCKED."""
    set_entry_aggressiveness(50, actor="test")
    stale = _quote(99.98, 100.0, ts=datetime(2020, 1, 1, tzinfo=UTC))
    admission = _admit(
        setup_q=52,
        entry_q=50,
        price=100.0,
        zone_low=99.0,
        zone_high=101.0,
        entry=100.0,
        stop=90.0,
        target=122.0,
        quote=stale,
    )
    assert admission.decision is AdmissionDecision.DATA_BLOCKED
    assert "DATA_BLOCKED" in admission.reason_codes
    assert "SETUP_COMPENSATED" not in admission.reason_codes
    assert admission.admitted is False


def test_h_hard_risk_block_forbids_buy() -> None:
    """H: compensatable setup with a hard risk/geometry block → BUY forbidden."""
    set_entry_aggressiveness(50, actor="test")
    admission = _admit(
        setup_q=52,
        entry_q=50,
        price=100.0,
        zone_low=99.0,
        zone_high=101.0,
        entry=100.0,
        stop=100.0,
        target=122.0,
    )
    assert admission.decision is not AdmissionDecision.BUY_ALLOWED
    assert admission.admitted is False
    assert "SETUP_COMPENSATED" not in admission.reason_codes
    assert "BUY_ALLOWED" not in admission.reason_codes


def test_i_live_does_not_compensate(monkeypatch) -> None:
    """I: same candidate in LIVE → compensation is not applied."""
    result = _comp(paper=False, setup_score=52, setup_floor=53, rr=2.2)
    assert result.applied is False

    monkeypatch.setattr("trading.admission_relaxation.is_paper_broker", lambda: False)
    set_entry_aggressiveness(50, actor="test")
    admission = _admit(
        setup_q=52,
        entry_q=50,
        price=100.0,
        zone_low=99.0,
        zone_high=101.0,
        entry=100.0,
        stop=90.0,
        target=122.0,
    )
    assert "SETUP_COMPENSATED" not in admission.reason_codes
    assert "SETUP_BELOW_FLOOR" in admission.reason_codes
    # LIVE still uses the historical floor (55 at step 50), so 52 is a miss.
    from trading.entry_policy import get_entry_thresholds

    assert get_entry_thresholds().min_setup_quality == 55


def test_tsla_borderline_is_compensated_not_setup_floor_wait() -> None:
    set_entry_aggressiveness(50, actor="test")
    price = 352.10
    stop = 335.998
    target = 384.305
    assert (
        planned_long_rr(price, stop, target) == 2.0
        or (planned_long_rr(price, stop, target) or 0) >= 2.0
    )
    admission = _admit(
        setup_q=52,
        entry_q=50,
        price=price,
        zone_low=345.198,
        zone_high=352.10,
        entry=price,
        stop=stop,
        target=target,
        atr=4.0,
    )
    assert "SETUP_BELOW_FLOOR" not in admission.reason_codes
    assert "SETUP_COMPENSATED" in admission.reason_codes
    assert admission.decision in {
        AdmissionDecision.BUY_ALLOWED,
        AdmissionDecision.WAIT,
        AdmissionDecision.NO_TRADE,
        AdmissionDecision.DATA_BLOCKED,
    }
    if admission.decision is not AdmissionDecision.BUY_ALLOWED:
        assert "BUY_ALLOWED" not in admission.reason_codes
