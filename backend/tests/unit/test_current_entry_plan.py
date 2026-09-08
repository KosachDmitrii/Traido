"""A valid nearby entry must not be forced to wait for an exact SMA20 touch."""

from __future__ import annotations

from agents.trader.entry import run_entry
from agents.trader.types import TraderBundle
from core.enums import EntryDecision, MarketRegimeLabel, SessionCohort, Timeframe
from core.schemas import MarketAssessment, NewsAssessment, TechnicalAssessment
from tests.unit.test_entry_timing_f3 import _snap
from trading.current_entry_plan import current_entry_is_eligible, current_entry_zone
from trading.entry_policy import set_entry_aggressiveness, thresholds_for


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
    assert bundle._entry_decision is not None
    assert bundle._entry_decision.entry_decision is EntryDecision.BUY_NOW
    assert float(bundle._entry_decision.entry_zone_high) == 100.8
    assert "CURRENT_ENTRY_NEAR_SMA20" in bundle._entry_decision.reasons


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
