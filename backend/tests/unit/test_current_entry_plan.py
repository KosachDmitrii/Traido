"""A valid nearby entry must not be forced to wait for an exact SMA20 touch."""

from __future__ import annotations

from decimal import Decimal

from agents.trader.entry import run_entry
from agents.trader.orchestrator import _build_candidate
from agents.trader.risk_plan import run_risk_plan
from agents.trader.types import TraderBundle
from core.enums import (
    AdmissionDecision,
    EntryDecision,
    MarketRegimeLabel,
    SessionCohort,
    Timeframe,
)
from core.schemas import MarketAssessment, NewsAssessment, Quote, TechnicalAssessment
from tests.unit.test_entry_timing_f3 import _snap
from trading.current_entry_plan import current_entry_is_eligible, current_entry_zone
from trading.entry_policy import set_entry_aggressiveness, thresholds_for
from trading.trade_admission import evaluate_trade_admission
from trading.zone_arrival import ArrivalType, ZoneArrivalFacts


def test_nearby_non_extended_price_can_be_evaluated_as_current_entry() -> None:
    th = thresholds_for(50)

    assert current_entry_is_eligible(price=100.8, sma20=100.0, atr=1.0, thresholds=th)


def test_atr_extension_still_blocks_current_entry_geometry() -> None:
    th = thresholds_for(50)

    assert not current_entry_is_eligible(price=103.0, sma20=100.0, atr=1.0, thresholds=th)


def test_percentage_extension_still_blocks_current_entry_geometry() -> None:
    th = thresholds_for(50)

    assert not current_entry_is_eligible(price=104.0, sma20=100.0, atr=10.0, thresholds=th)


def test_current_entry_zone_contains_the_evaluated_price() -> None:
    th = thresholds_for(50)

    low, high = current_entry_zone(price=100.8, atr=1.0, thresholds=th)

    assert low < high
    assert float(high) == 100.8
    assert float(high - low) >= th.zone_min_width_atr


def test_nearby_price_below_sma_can_use_current_geometry() -> None:
    th = thresholds_for(50)

    assert current_entry_is_eligible(price=99.8, sma20=100.0, atr=1.0, thresholds=th)


def test_trader_entry_publishes_buy_now_geometry_at_a_valid_current_price(monkeypatch) -> None:
    monkeypatch.setattr(
        "trading.entry_timing.session_cohort",
        lambda _ts=None: SessionCohort.RTH,
    )
    set_entry_aggressiveness(100, actor="test")
    bundle = TraderBundle(
        symbol="TEST",
        features={
            Timeframe.H1: _snap(
                close=100.8,
                sma20=100.0,
                vwap=100.3,
                atr=1.0,
                support=[98.5],
                resistance=[110.0],
                roc=0.5,
            )
        },
        technical=TechnicalAssessment(
            symbol="TEST", trend="bullish", score=90, reasons=["uptrend"]
        ),
        news=NewsAssessment(symbol="TEST", sentiment="positive", score=80, reasons=["checked"]),
        market=MarketAssessment(
            regime=MarketRegimeLabel.BULLISH,
            score=85,
            risk_posture="risk_on",
            reasons=["confirmed"],
        ),
    )

    result = run_entry(bundle)

    assert result.detail == "BUY_NOW"
    assert bundle._planned is not None and bundle._planned[0] == 100.8
    assert bundle._planned[1] <= 98.5 * 0.995
    assert bundle._entry_decision is not None
    assert bundle._entry_decision.entry_decision is EntryDecision.BUY_NOW
    assert float(bundle._entry_decision.entry_zone_high) == 100.8
    assert "CURRENT_ENTRY_NEAR_SMA20" in bundle._entry_decision.reasons


def test_current_buy_geometry_survives_full_trade_admission(monkeypatch) -> None:
    """Regression for the production JNJ failure after TraderDeskCandidate."""
    from datetime import UTC, datetime
    from uuid import uuid4

    monkeypatch.setattr(
        "trading.entry_timing.session_cohort",
        lambda _ts=None: SessionCohort.RTH,
    )
    set_entry_aggressiveness(100, actor="test")
    bundle = TraderBundle(
        symbol="TEST",
        features={
            Timeframe.H1: _snap(
                close=99.8,
                sma20=100.0,
                vwap=99.6,
                atr=1.0,
                rvol=0.8,
                support=[98.7],
                resistance=[106.0],
                roc=0.5,
            )
        },
        technical=TechnicalAssessment(
            symbol="TEST", trend="bullish", score=90, reasons=["uptrend"]
        ),
        news=NewsAssessment(symbol="TEST", sentiment="positive", score=80, reasons=["checked"]),
        market=MarketAssessment(
            regime=MarketRegimeLabel.BULLISH,
            score=85,
            risk_posture="risk_on",
            reasons=["confirmed"],
        ),
    )

    entry_step = run_entry(bundle)
    assert entry_step.detail == "BUY_NOW"
    assert run_risk_plan(bundle).ok
    candidate = _build_candidate(bundle, run_id=uuid4())
    assert bundle._entry_decision is not None
    assert bundle._entry_decision.target is not None
    assert candidate.target_model == bundle._entry_decision.target.model
    assert candidate.target_reachability == bundle._entry_decision.target.reachability
    arrival = ZoneArrivalFacts(
        score=80,
        arrival_type=ArrivalType.HEALTHY_PULLBACK,
        arrival_speed_pct=0.2,
        arrival_speed_atr=0.2,
        atr_velocity=0.1,
        bars_to_zone=4,
        red_bar_ratio=0.4,
        consecutive_red_bars=1,
        largest_red_bar_atr=0.3,
        sell_volume_ratio=0.8,
        volume_acceleration=0.9,
        gap_down_pct=None,
        crash_velocity=False,
        structural_damage=False,
        reason_codes=[],
    )
    admission = evaluate_trade_admission(
        bundle=bundle._entry_decision,
        candidate=candidate,
        quote=Quote(
            symbol="TEST",
            bid=Decimal("99.79"),
            ask=Decimal("99.80"),
            ts=datetime.now(UTC),
            source="test",
        ),
        zone_arrival=arrival,
    )

    assert admission.decision is AdmissionDecision.BUY_ALLOWED, admission.reason_codes
    assert "ENTRY_OUTSIDE_ALLOWED_ZONE" not in admission.reason_codes
    assert "ATR_ONLY_STOP" not in admission.reason_codes
    assert "TARGET_UNREALISTIC" not in admission.reason_codes


def test_current_timing_without_structural_geometry_stays_wait(monkeypatch) -> None:
    """High timing scores cannot publish a BUY that admission must reject."""
    monkeypatch.setattr(
        "trading.entry_timing.session_cohort",
        lambda _ts=None: SessionCohort.RTH,
    )
    snap = _snap(
        close=100.8,
        sma20=100.0,
        vwap=100.3,
        atr=1.0,
        support=[98.5],
        resistance=[110.0],
        roc=0.5,
    )
    snap.support = []
    bundle = TraderBundle(
        symbol="TEST",
        features={Timeframe.H1: snap},
        technical=TechnicalAssessment(
            symbol="TEST", trend="bullish", score=90, reasons=["uptrend"]
        ),
        news=NewsAssessment(symbol="TEST", sentiment="positive", score=80, reasons=["checked"]),
        market=MarketAssessment(
            regime=MarketRegimeLabel.BULLISH,
            score=85,
            risk_posture="risk_on",
            reasons=["confirmed"],
        ),
    )

    result = run_entry(bundle)

    assert result.detail == "WAIT"
    assert bundle._entry_decision is not None
    assert bundle._entry_decision.entry_decision is EntryDecision.WAIT_FOR_ENTRY
    assert "CURRENT_GEOMETRY_NOT_ADMISSIBLE" in bundle._entry_decision.reasons
    assert "ATR_ONLY_STOP" in bundle._entry_decision.reasons


def test_extended_price_keeps_the_pullback_plan(monkeypatch) -> None:
    monkeypatch.setattr(
        "trading.entry_timing.session_cohort",
        lambda _ts=None: SessionCohort.RTH,
    )
    # Even the weakest confirmation step must not widen candidate geometry.
    set_entry_aggressiveness(100, actor="test")
    bundle = TraderBundle(
        symbol="TEST",
        features={Timeframe.H1: _snap(close=103.0, sma20=100.0, vwap=100.0, atr=1.0)},
        technical=TechnicalAssessment(
            symbol="TEST", trend="bullish", score=90, reasons=["uptrend"]
        ),
    )

    run_entry(bundle)

    assert bundle._planned is not None and bundle._planned[0] == 100.0
