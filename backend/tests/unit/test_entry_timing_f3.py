"""F3 EntryTiming / TargetModel / WAIT regressions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from core.enums import (
    EntryDecision,
    EntryWatchStatus,
    InstrumentThesis,
    MarketRegimeLabel,
    Timeframe,
    TradeAction,
)
from core.schemas import FeatureSnapshot, MarketAssessment
from trading.attribution import build_attribution
from trading.entry_quality import decide_entry, score_entry_quality
from trading.entry_timing import (
    ATR_EXTENSION_HIGH,
    detect_chasing,
    evaluate_timing,
)
from trading.entry_watch_eval import (
    WaitRevalidationError,
    observe_price,
    revalidate_triggered_watch,
)
from trading.entry_watches import ENTRY_WATCHES
from trading.target_model import build_target_plan


def _snap(
    *,
    close: float = 100.0,
    sma20: float = 99.0,
    ema50: float = 98.0,
    vwap: float = 99.5,
    atr: float = 1.0,
    rvol: float = 1.6,
    support: list[float] | None = None,
    resistance: list[float] | None = None,
    roc: float = 0.4,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol="TEST",
        timeframe=Timeframe.H1,
        computed_at=datetime.now(UTC),
        indicators={
            "close": close,
            "sma_20": sma20,
            "ema_50": ema50,
            "ema_200": 90.0,
            "ema50_above_ema200": True,
            "vwap": vwap,
            "atr_14": atr,
            "relative_volume": rvol,
            "roc_10": roc,
            "rsi_14": 52.0,
        },
        candlestick_patterns={},
        chart_patterns={"structure": "uptrend"},
        support=[Decimal(str(s)) for s in (support or [97.0])],
        resistance=[Decimal(str(r)) for r in (resistance or [101.0])],
    )


def test_high_atr_extension_is_chase() -> None:
    facts = evaluate_timing(
        _snap(close=102.0, sma20=100.0, atr=1.0, vwap=100.0),
        signal_price=100.0,
        planned_entry=100.0,
        planned_stop=98.5,
        planned_target=103.0,
    )
    assert facts.atr_extension is not None and facts.atr_extension >= 1.5
    codes = detect_chasing(facts)
    assert ATR_EXTENSION_HIGH in codes


def test_bullish_thesis_plus_extension_is_wait_not_buy() -> None:
    facts = evaluate_timing(
        _snap(close=102.0, sma20=100.0, atr=1.0, vwap=100.0, resistance=[102.2]),
        signal_price=100.0,
        planned_entry=100.0,
        planned_stop=98.5,
        planned_target=103.0,
    )
    market = MarketAssessment(
        regime=MarketRegimeLabel.BULLISH,
        score=80,
        risk_posture="risk_on",
        reasons=["stub"],
    )
    bundle = decide_entry(
        InstrumentThesis.BULLISH,
        facts,
        market=market,
        technical_score=88,
    )
    assert bundle.thesis is InstrumentThesis.BULLISH
    assert bundle.entry_decision is not EntryDecision.BUY_NOW
    assert bundle.entry_decision in {
        EntryDecision.WAIT_FOR_ENTRY,
        EntryDecision.NO_TRADE,
    }
    assert ATR_EXTENSION_HIGH in bundle.chase_reasons


def test_strong_momentum_cannot_override_two_atr_extension() -> None:
    """Required F3 regression: momentum + bullish + high tech score ≠ BUY_NOW."""
    facts = evaluate_timing(
        _snap(close=102.0, sma20=100.0, atr=1.0, vwap=100.0, roc=3.0),
        signal_price=100.0,
        planned_entry=100.0,
        planned_stop=98.5,
        planned_target=104.0,
    )
    assert facts.atr_extension is not None and 1.8 <= facts.atr_extension <= 2.5
    market = MarketAssessment(
        regime=MarketRegimeLabel.BULLISH,
        score=90,
        risk_posture="risk_on",
        reasons=["hot"],
    )
    bundle = decide_entry(
        InstrumentThesis.BULLISH,
        facts,
        market=market,
        technical_score=92,
    )
    assert bundle.entry_decision is not EntryDecision.BUY_NOW
    assert bundle.entry_decision is EntryDecision.WAIT_FOR_ENTRY


def test_resistance_too_close_forces_wait() -> None:
    facts = evaluate_timing(
        _snap(close=100.0, sma20=99.8, atr=1.0, vwap=99.9, resistance=[100.2]),
        signal_price=100.0,
        planned_entry=100.0,
        planned_stop=98.5,
        planned_target=103.0,
    )
    bundle = decide_entry(InstrumentThesis.BULLISH, facts, technical_score=80)
    assert "RESISTANCE_TOO_CLOSE" in bundle.chase_reasons
    assert bundle.entry_decision in {EntryDecision.WAIT_FOR_ENTRY, EntryDecision.NO_TRADE}


def test_2r_beyond_resistance_not_blindly_accepted() -> None:
    facts = evaluate_timing(
        _snap(close=100.0, sma20=99.5, atr=1.0, resistance=[100.6]),
        planned_entry=100.0,
        planned_stop=99.0,
        planned_target=102.0,
    )
    plan = build_target_plan(
        entry=Decimal(100),
        stop=Decimal(99),
        facts=facts,
        min_rr=2.0,
    )
    assert plan.two_r_target == Decimal("102.0000") or float(plan.two_r_target) == 102.0
    # Structure / atr should win over unreachable 2R.
    assert plan.model != "2R" or plan.reachability.value in {
        "ambitious",
        "unrealistic",
        "insufficient_data",
    }
    assert float(plan.price) <= float(plan.two_r_target or plan.price)


def test_entry_quality_is_deterministic_decomposition() -> None:
    facts = evaluate_timing(_snap())
    b = score_entry_quality(facts, technical_score=80)
    d = b.as_dict()
    assert set(d) >= {"vwap_location", "atr_extension", "pullback_quality"}
    assert 0 <= b.total <= 100


def test_wait_observe_does_not_execute() -> None:
    market = MarketAssessment(
        regime=MarketRegimeLabel.BULLISH,
        score=70,
        risk_posture="risk_on",
        reasons=["not configured"],
    )
    facts = evaluate_timing(
        _snap(close=102.0, sma20=100.0, atr=1.0, vwap=100.0),
        signal_price=100.0,
        planned_entry=100.0,
        planned_stop=98.5,
        planned_target=103.0,
    )
    bundle = decide_entry(InstrumentThesis.BULLISH, facts, market=market, technical_score=88)
    assert bundle.entry_decision is EntryDecision.WAIT_FOR_ENTRY

    from core.schemas import TradeCandidate

    cand = TradeCandidate(
        symbol="TEST",
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal(100),
        stop=Decimal("98.5"),
        target=Decimal(103),
        risk_reward=2.0,
        reasons=["x"],
        strategy_version="test",
        thesis=InstrumentThesis.BULLISH,
        entry_decision=EntryDecision.WAIT_FOR_ENTRY,
        entry_quality=bundle.entry_quality,
        signal_price=Decimal(100),
        entry_zone_low=bundle.entry_zone_low,
        entry_zone_high=bundle.entry_zone_high,
    )
    ENTRY_WATCHES.clear()
    watch = ENTRY_WATCHES.create_from_bundle(cand, bundle)
    assert watch.status is EntryWatchStatus.WAITING
    triggered = observe_price(watch, float(bundle.entry_zone_low) + 0.01)
    assert triggered.status is EntryWatchStatus.TRIGGERED


def test_wait_revalidation_without_quote_is_no_trade() -> None:
    facts = evaluate_timing(
        _snap(close=102.0, sma20=100.0, atr=1.0, vwap=100.0),
        signal_price=100.0,
        planned_entry=100.0,
        planned_stop=98.5,
        planned_target=103.0,
    )
    bundle = decide_entry(InstrumentThesis.BULLISH, facts, technical_score=88)
    from core.schemas import TradeCandidate

    cand = TradeCandidate(
        symbol="TEST",
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal(100),
        stop=Decimal("98.5"),
        target=Decimal(103),
        risk_reward=2.0,
        reasons=["x"],
        strategy_version="test",
        entry_zone_low=bundle.entry_zone_low,
        entry_zone_high=bundle.entry_zone_high,
        signal_price=Decimal(100),
    )
    ENTRY_WATCHES.clear()
    watch = ENTRY_WATCHES.create_from_bundle(cand, bundle)
    watch = observe_price(watch, float(bundle.entry_zone_low))
    assert watch.status is EntryWatchStatus.TRIGGERED
    decision = revalidate_triggered_watch(watch, exec_snap=_snap(), quote=None)
    assert decision is EntryDecision.NO_TRADE


def test_wait_revalidation_rejects_non_triggered() -> None:
    facts = evaluate_timing(_snap(close=102.0, sma20=100.0, atr=1.0, vwap=100.0))
    bundle = decide_entry(InstrumentThesis.BULLISH, facts, technical_score=88)
    from core.schemas import TradeCandidate

    cand = TradeCandidate(
        symbol="TEST",
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal(100),
        stop=Decimal("98.5"),
        target=Decimal(103),
        risk_reward=2.0,
        reasons=["x"],
        strategy_version="test",
        entry_zone_low=bundle.entry_zone_low,
        entry_zone_high=bundle.entry_zone_high,
        signal_price=Decimal(100),
    )
    ENTRY_WATCHES.clear()
    watch = ENTRY_WATCHES.create_from_bundle(cand, bundle)
    with pytest.raises(WaitRevalidationError):
        revalidate_triggered_watch(watch, exec_snap=_snap(), quote=None)


def test_attribution_signal_to_fill_metrics() -> None:
    t0 = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)
    t1 = datetime(2026, 8, 31, 14, 5, tzinfo=UTC)
    attr = build_attribution(
        symbol="AAPL",
        opportunity_id=uuid4(),
        signal_detected_at=t0,
        signal_price=Decimal(100),
        opportunity_published_at=t0,
        published_price=Decimal(100),
        operator_approved_at=t1,
        approval_price=Decimal("100.40"),
        broker_submitted_at=t1,
        submit_reference_price=Decimal("100.40"),
        broker_filled_at=t1,
        fill_price=Decimal("100.52"),
        atr=0.55,
        expected_60m_move_pct=0.55,
    )
    assert attr.signal_to_fill_bps is not None
    assert abs(attr.signal_to_fill_bps - 52.0) < 0.1
    assert attr.expected_move_consumed_fraction is not None
    assert attr.expected_move_consumed_fraction > 0.9
