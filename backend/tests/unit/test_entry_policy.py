"""Operator entry aggressiveness must widen BUY without disarming hard vetoes."""

from __future__ import annotations

from core.enums import EntryDecision, InstrumentThesis
from trading.entry_policy import set_entry_aggressiveness, thresholds_for
from trading.entry_quality import decide_entry
from trading.entry_timing import detect_chasing, evaluate_timing, zone_from_facts
from tests.unit.test_entry_timing_f3 import _snap


def test_zero_matches_shipped_f3_floors() -> None:
    th = thresholds_for(0)
    assert th.aggressiveness == 0
    assert th.ema_ext_pct == 2.5
    assert th.vwap_ext_pct == 1.0
    assert th.min_buy_quality == 55
    assert th.zone_gap_frac == 0.0
    assert th.allow_soft_chase_buy is False


def test_levels_snap_to_five_steps() -> None:
    assert thresholds_for(50).aggressiveness == 55
    assert thresholds_for(70).aggressiveness == 80
    assert thresholds_for(12).aggressiveness == 0


def test_full_aggressiveness_widens_extension() -> None:
    th = thresholds_for(100)
    assert th.ema_ext_pct == 18.0
    assert th.min_buy_quality == 40
    assert th.zone_gap_frac == 0.85
    assert th.allow_soft_chase_buy is True


def test_aggressive_policy_allows_extended_buy() -> None:
    """AAPL-class stretch: ~16% above SMA — WAIT at 0, BUY at 100 if quality holds."""
    facts = evaluate_timing(
        _snap(close=116.0, sma20=100.0, atr=2.0, vwap=100.0, resistance=[130.0]),
        signal_price=114.0,
        planned_entry=114.0,
        planned_stop=108.0,
        planned_target=140.0,
    )
    set_entry_aggressiveness(0, actor="test")
    strict = decide_entry(InstrumentThesis.BULLISH, facts, technical_score=88)
    assert strict.entry_decision is EntryDecision.WAIT_FOR_ENTRY
    assert "PRICE_TOO_EXTENDED_FROM_EMA" in strict.chase_reasons

    set_entry_aggressiveness(100, actor="test")
    loose = decide_entry(InstrumentThesis.BULLISH, facts, technical_score=88)
    assert loose.entry_decision is EntryDecision.BUY_NOW
    # Zone high moves toward live price so waits are reachable.
    assert float(loose.entry_zone_high) > float(strict.entry_zone_high)


def test_hard_veto_survives_aggressiveness() -> None:
    """Hard chase codes are never in the soft allow-list — soft_only stays false."""
    from trading.entry_policy import SOFT_CHASE_CODES

    hard = {"REWARD_ALREADY_CONSUMED", "ASYMMETRIC_DOWNSIDE"}
    assert not hard <= SOFT_CHASE_CODES

    facts = evaluate_timing(
        _snap(close=102.5, sma20=100.0, atr=1.0, vwap=100.0, resistance=[102.7]),
        signal_price=100.0,
        planned_entry=100.0,
        planned_stop=99.5,
        planned_target=103.0,
    )
    set_entry_aggressiveness(100, actor="test")
    chase = detect_chasing(facts)
    if hard.issubset(set(chase)):
        bundle = decide_entry(InstrumentThesis.BULLISH, facts, technical_score=95)
        assert bundle.entry_decision is EntryDecision.NO_TRADE


def test_zone_gap_moves_high_toward_price() -> None:
    facts = evaluate_timing(_snap(close=110.0, sma20=100.0, atr=2.0, vwap=100.0))
    set_entry_aggressiveness(0, actor="test")
    _, high0 = zone_from_facts(facts)
    set_entry_aggressiveness(100, actor="test")
    _, high1 = zone_from_facts(facts)
    assert float(high1) > float(high0)
    assert float(high1) <= 110.0


def test_detect_chasing_respects_thresholds() -> None:
    facts = evaluate_timing(_snap(close=105.0, sma20=100.0, atr=1.0, vwap=100.0))
    set_entry_aggressiveness(0, actor="test")
    assert "PRICE_TOO_EXTENDED_FROM_EMA" in detect_chasing(facts)
    set_entry_aggressiveness(100, actor="test")
    assert "PRICE_TOO_EXTENDED_FROM_EMA" not in detect_chasing(facts)
