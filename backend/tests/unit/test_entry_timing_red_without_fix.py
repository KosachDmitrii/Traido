"""Red-without-fix for F3 extension veto.

Disarms ATR via a fake thresholds object; the ATR code must disappear.
"""

from __future__ import annotations

from dataclasses import replace

from core.enums import EntryDecision, InstrumentThesis, MarketRegimeLabel
from core.schemas import MarketAssessment
from tests.unit.test_entry_timing_f3 import _snap
from trading.entry_policy import thresholds_for
from trading.entry_quality import decide_entry
from trading.entry_timing import detect_chasing, evaluate_timing


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
    assert (
        decide_entry(
            InstrumentThesis.BULLISH, facts, market=market, technical_score=92
        ).entry_decision
        is not EntryDecision.BUY_NOW
    )

    base = thresholds_for(0)
    disarmed = replace(
        base,
        vwap_ext_pct=99.0,
        ema_ext_pct=99.0,
        atr_ext_max=99.0,
        drift_high_pct=99.0,
        zone_gap_frac=0.0,
        allow_soft_chase_buy=False,
    )
    codes = detect_chasing(facts, thresholds=disarmed)
    assert "ATR_EXTENSION_HIGH" not in codes
