"""Chase detection — single source for extension / reward-consumed facts."""

from __future__ import annotations

from typing import Any

from core.schemas import ChaseFacts, EntryTimingFacts
from trading.entry_policy import get_entry_thresholds
from trading.entry_timing import detect_chasing

HARD_CHASE_LIMIT = 80

# Per-code contribution to chase score (0–100 aggregate).
_CHASE_SCORE_WEIGHTS: dict[str, int] = {
    "PRICE_TOO_EXTENDED_FROM_VWAP": 22,
    "PRICE_TOO_EXTENDED_FROM_EMA": 25,
    "ATR_EXTENSION_HIGH": 28,
    "IMPULSE_ALREADY_MATURE": 20,
    "RESISTANCE_TOO_CLOSE": 18,
    "REWARD_ALREADY_CONSUMED": 35,
    "ASYMMETRIC_DOWNSIDE": 30,
    "SIGNAL_TO_ENTRY_DRIFT_HIGH": 22,
    "NORMAL_RETRACE_EXCEEDS_STOP": 40,
    "IMPULSE_WEAK": 15,
    "PULLBACK_TOO_DEEP": 45,
    "PULLBACK_TOO_SHALLOW": 12,
    "PULLBACK_EXHAUSTED": 18,
    "PULLBACK_VOL_HEAVY": 20,
}


def compute_chase_facts(
    facts: EntryTimingFacts,
    *,
    zone_high: float | None = None,
    thresholds: Any | None = None,
) -> ChaseFacts:
    th = thresholds if thresholds is not None else get_entry_thresholds()
    codes = detect_chasing(facts, thresholds=th)
    score = 0
    for code in codes:
        score += _CHASE_SCORE_WEIGHTS.get(code, 10)
    score = min(100, score)

    zone_ext = None
    if zone_high is not None and facts.current_price > zone_high:
        atr = facts.atr or max(facts.current_price * 0.01, 0.01)
        zone_ext = (facts.current_price - zone_high) / atr

    reward_consumed = None
    if (
        facts.signal_to_current_drift_pct is not None
        and facts.remaining_expected_reward_pct is not None
        and facts.remaining_expected_reward_pct > 0
    ):
        reward_consumed = max(
            0.0,
            facts.signal_to_current_drift_pct / facts.remaining_expected_reward_pct,
        )

    return ChaseFacts(
        score=score,
        atr_extension=facts.atr_extension,
        vwap_extension=facts.distance_from_vwap_pct,
        zone_extension=zone_ext,
        reward_consumed_pct=reward_consumed,
        reason_codes=codes,
    )
