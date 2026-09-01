"""Red-without-fix for F3 extension veto.

Temporarily raises ATR_EXT_MAX so the dedicated chase code disappears; the
test must fail. Restored in finally.
"""

from __future__ import annotations

import trading.entry_timing as et
from core.enums import EntryDecision, InstrumentThesis, MarketRegimeLabel
from core.schemas import MarketAssessment
from tests.unit.test_entry_timing_f3 import _snap
from trading.entry_quality import decide_entry
from trading.entry_timing import evaluate_timing


def test_extension_veto_red_without_fix() -> None:
    facts = evaluate_timing(
        _snap(close=102.0, sma20=100.0, atr=1.0, vwap=100.0),
        signal_price=100.0,
        planned_entry=100.0,
        planned_stop=98.5,
        planned_target=103.0,
    )
    market = MarketAssessment(
        regime=MarketRegimeLabel.BULLISH,
        score=90,
        risk_posture="risk_on",
        reasons=["x"],
    )
    # Green: extension veto active → not BUY_NOW
    assert decide_entry(InstrumentThesis.BULLISH, facts, market=market, technical_score=92).entry_decision is not EntryDecision.BUY_NOW

    original = et.ATR_EXT_MAX
    et.ATR_EXT_MAX = 99.0
    try:
        # Also relax sibling chase thresholds so only ATR mattered.
        old_vwap, old_ema, old_drift = et.VWAP_EXT_PCT, et.EMA_EXT_PCT, et.DRIFT_HIGH_PCT
        et.VWAP_EXT_PCT = 99.0
        et.EMA_EXT_PCT = 99.0
        et.DRIFT_HIGH_PCT = 99.0
        et.RESISTANCE_TOO_CLOSE_PCT = 0.0
        try:
            # With veto disarmed, high extension alone must not force WAIT via ATR code.
            codes = et.detect_chasing(facts)
            assert "ATR_EXTENSION_HIGH" not in codes
        finally:
            et.VWAP_EXT_PCT, et.EMA_EXT_PCT, et.DRIFT_HIGH_PCT = old_vwap, old_ema, old_drift
            et.RESISTANCE_TOO_CLOSE_PCT = 0.40
    finally:
        et.ATR_EXT_MAX = original
