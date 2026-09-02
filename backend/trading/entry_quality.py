"""Deterministic EntryQualityScore 0–100. Claude never generates this number."""

from __future__ import annotations

from core.enums import EntryDecision, InstrumentThesis
from core.schemas import (
    EntryDecisionBundle,
    EntryQualityBreakdown,
    EntryTimingFacts,
    MarketAssessment,
    TargetPlan,
)
from trading.entry_policy import SOFT_CHASE_CODES, get_entry_thresholds
from trading.entry_timing import (
    ATR_EXTENSION_HIGH,
    PULLBACK_TOO_DEEP,
    detect_chasing,
    zone_from_facts,
)
from trading.setup_quality import score_setup_quality


def _clamp(n: int) -> int:
    return max(0, min(100, n))


def score_entry_quality(
    facts: EntryTimingFacts,
    *,
    market: MarketAssessment | None = None,
    technical_score: int | None = None,
) -> EntryQualityBreakdown:
    """Entry timing quality — whether the current price is a good entry."""
    if facts.distance_from_fast_ema_pct is None:
        price_loc = 50
    elif facts.distance_from_fast_ema_pct <= 0.5:
        price_loc = 90
    elif facts.distance_from_fast_ema_pct <= 1.5:
        price_loc = 70
    elif facts.distance_from_fast_ema_pct <= 2.5:
        price_loc = 45
    else:
        price_loc = 15

    if facts.distance_from_vwap_pct is None:
        vwap_loc = 50
    elif facts.distance_from_vwap_pct <= 0.2:
        vwap_loc = 88
    elif facts.distance_from_vwap_pct <= 0.8:
        vwap_loc = 65
    elif facts.distance_from_vwap_pct <= 1.2:
        vwap_loc = 35
    else:
        vwap_loc = 15

    if facts.atr_extension is None:
        atr_sc = 50
    elif facts.atr_extension <= 0.5:
        atr_sc = 90
    elif facts.atr_extension <= 1.0:
        atr_sc = 70
    elif facts.atr_extension <= 1.5:
        atr_sc = 40
    else:
        atr_sc = 15

    if facts.pullback_depth_pct is None and facts.retracement_pct is None:
        pullback = 40
    elif facts.retracement_pct is not None:
        r = facts.retracement_pct
        if 0.38 <= r <= 0.62:
            pullback = 90
        elif 0.30 <= r < 0.38 or 0.62 < r <= 0.78:
            pullback = 65
        elif r > 0.786:
            pullback = 15
        elif r < 0.20:
            pullback = 40
        else:
            pullback = 50
    elif facts.pullback_depth_pct is not None and 0.3 <= facts.pullback_depth_pct <= 2.5:
        pullback = 85
    elif facts.pullback_depth_pct is not None and facts.pullback_depth_pct < 0.3:
        pullback = 45
    else:
        pullback = 30

    if facts.remaining_expected_reward_pct is None:
        reward = 50
    elif facts.remaining_expected_reward_pct >= 0.8:
        reward = 85
    elif facts.remaining_expected_reward_pct >= 0.4:
        reward = 60
    elif facts.remaining_expected_reward_pct > 0:
        reward = 30
    else:
        reward = 10

    if facts.distance_to_support_pct is None:
        support = 45
    elif facts.distance_to_support_pct <= 1.0:
        support = 80
    elif facts.distance_to_support_pct <= 2.0:
        support = 60
    else:
        support = 35

    if facts.distance_to_resistance_pct is None:
        resist = 50
    elif facts.distance_to_resistance_pct >= 1.0:
        resist = 85
    elif facts.distance_to_resistance_pct >= 0.5:
        resist = 55
    else:
        resist = 15

    mom = 50
    if facts.short_term_momentum_pct is not None:
        if -0.5 <= facts.short_term_momentum_pct <= 1.5:
            mom = 75
        elif facts.short_term_momentum_pct > 2.5:
            mom = 25
        else:
            mom = 55

    market_al = 70
    if market is not None:
        if market.risk_posture == "risk_off" or market.regime.value in {
            "bearish",
            "risk_off",
            "high_volatility",
        }:
            market_al = 25
        elif market.regime.value in {"bullish", "risk_on"}:
            market_al = 90
        else:
            market_al = 60

    if facts.signal_to_current_drift_pct is None:
        drift = 70
    elif facts.signal_to_current_drift_pct <= 0.15:
        drift = 90
    elif facts.signal_to_current_drift_pct <= 0.40:
        drift = 55
    else:
        drift = 15

    if technical_score is not None:
        price_loc = _clamp(round(0.85 * price_loc + 0.15 * technical_score))

    # Legacy breakdown fields retained for API compat; setup uses SetupQualityBreakdown.
    impulse_q = 50
    retrace_q = pullback
    vol = 50

    return EntryQualityBreakdown(
        price_location=_clamp(price_loc),
        vwap_location=_clamp(vwap_loc),
        atr_extension=_clamp(atr_sc),
        pullback_quality=_clamp(pullback),
        remaining_reward=_clamp(reward),
        support_structure=_clamp(support),
        resistance_structure=_clamp(resist),
        short_term_momentum=_clamp(mom),
        volume_confirmation=_clamp(vol),
        market_alignment=_clamp(market_al),
        signal_drift=_clamp(drift),
        impulse_quality=_clamp(impulse_q),
        retracement_quality=_clamp(retrace_q),
    )


def entry_quality_total(breakdown: EntryQualityBreakdown) -> int:
    """Entry-only score — never averaged with setup quality."""
    vals = [
        breakdown.price_location,
        breakdown.vwap_location,
        breakdown.atr_extension,
        breakdown.pullback_quality,
        breakdown.remaining_reward,
        breakdown.short_term_momentum,
        breakdown.signal_drift,
        breakdown.resistance_structure,
        breakdown.liquidity_spread,
    ]
    return round(sum(vals) / len(vals)) if vals else 0


def decide_entry(
    thesis: InstrumentThesis,
    facts: EntryTimingFacts,
    *,
    market: MarketAssessment | None = None,
    technical_score: int | None = None,
    news_score: int | None = None,
    target: TargetPlan | None = None,
    stop_price: float | None = None,
) -> EntryDecisionBundle:
    """Legacy candidate generator — TradeAdmission is the BUY authority."""
    th = get_entry_thresholds()
    min_quality = th.min_entry_quality
    setup_breakdown = score_setup_quality(
        facts, market=market, technical_score=technical_score, news_score=news_score
    )
    breakdown = score_entry_quality(facts, market=market, technical_score=technical_score)
    setup_quality = setup_breakdown.total
    quality = entry_quality_total(breakdown)
    chase = detect_chasing(facts, thresholds=th)
    reasons = list(chase)

    zone_low, zone_high = zone_from_facts(facts, thresholds=th)
    normal_retrace_exceeds = "NORMAL_RETRACE_EXCEEDS_STOP" in chase

    if thesis is not InstrumentThesis.BULLISH:
        decision = EntryDecision.NO_TRADE
        reasons.append("THESIS_NOT_BULLISH")
    else:
        soft_only = bool(chase) and set(chase) <= SOFT_CHASE_CODES
        aggressive_ok = th.allow_soft_chase_buy and soft_only and quality >= min_quality

        if (
            ATR_EXTENSION_HIGH in chase
            and quality < th.atr_extension_min_quality
            and not aggressive_ok
            or (
                chase and quality < min_quality + th.chase_wait_quality_buffer and not aggressive_ok
            )
        ):
            decision = EntryDecision.WAIT_FOR_ENTRY
        elif quality < min_quality:
            decision = EntryDecision.WAIT_FOR_ENTRY
            reasons.append("ENTRY_QUALITY_BELOW_THRESHOLD")
        elif setup_quality < th.min_setup_quality:
            decision = EntryDecision.WAIT_FOR_ENTRY
            reasons.append("SETUP_QUALITY_BELOW_THRESHOLD")
        elif chase and not aggressive_ok:
            decision = EntryDecision.WAIT_FOR_ENTRY
        else:
            decision = EntryDecision.BUY_NOW
            reasons.append(
                "ENTRY_QUALITY_ACCEPTABLE_AGGRESSIVE"
                if aggressive_ok and chase
                else "ENTRY_QUALITY_ACCEPTABLE"
            )

    if thesis is InstrumentThesis.BULLISH and {
        "REWARD_ALREADY_CONSUMED",
        "ASYMMETRIC_DOWNSIDE",
    }.issubset(set(chase)):
        decision = EntryDecision.NO_TRADE
        reasons.append("NO_EDGE_LEFT_AT_PRICE")
    elif thesis is InstrumentThesis.BULLISH and PULLBACK_TOO_DEEP in chase:
        if th.pullback_deep_no_trade:
            decision = EntryDecision.NO_TRADE
            reasons.append("PULLBACK_STRUCTURE_BROKEN")
        else:
            decision = EntryDecision.WAIT_FOR_ENTRY
            reasons.append("PULLBACK_DEEP_WAIT")

    from decimal import Decimal

    return EntryDecisionBundle(
        thesis=thesis,
        entry_decision=decision,
        entry_quality=quality,
        setup_quality=setup_quality,
        setup_breakdown=setup_breakdown,
        breakdown=breakdown,
        chase_reasons=chase,
        facts=facts,
        entry_zone_low=zone_low,
        entry_zone_high=zone_high,
        target=target,
        stop_price=Decimal(str(stop_price)) if stop_price else None,
        normal_retrace_exceeds_stop=normal_retrace_exceeds,
        reasons=reasons,
    )
