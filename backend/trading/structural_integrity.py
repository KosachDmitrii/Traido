"""Structural integrity — is the setup's price structure still valid?"""

from __future__ import annotations

from core.schemas import EntryTimingFacts, StructuralIntegrityFacts
from trading.entry_timing import (
    IMPULSE_WEAK,
    NORMAL_RETRACE_EXCEEDS_STOP,
    PULLBACK_TOO_DEEP,
)


def evaluate_structural_integrity(
    facts: EntryTimingFacts,
    *,
    chase_reasons: list[str] | None = None,
    deep_pullback_is_hard: bool = True,
) -> StructuralIntegrityFacts:
    codes = list(chase_reasons or [])
    reasons: list[str] = []

    impulse_valid = facts.impulse_grade not in {None, "C"} or IMPULSE_WEAK not in codes
    if not impulse_valid:
        reasons.append("IMPULSE_WEAK")

    retracement_valid = PULLBACK_TOO_DEEP not in codes
    if not retracement_valid:
        reasons.append("PULLBACK_TOO_DEEP")

    support_valid = True
    if facts.nearest_support is not None and facts.current_price < facts.nearest_support:
        support_valid = False
        reasons.append("SUPPORT_BROKEN")

    vwap_valid = True
    if facts.distance_from_vwap_pct is not None and facts.distance_from_vwap_pct < -2.5:
        vwap_valid = False
        reasons.append("VWAP_BREAKDOWN")

    swing_valid = NORMAL_RETRACE_EXCEEDS_STOP not in codes
    if not swing_valid:
        reasons.append("NORMAL_RETRACE_EXCEEDS_STOP")

    # On softer/weak entry policy, a deep pullback is WAIT material — not a
    # structural hard kill that deletes the wait card before it is drawn.
    hard_damage = (
        (PULLBACK_TOO_DEEP in codes and deep_pullback_is_hard)
        or NORMAL_RETRACE_EXCEEDS_STOP in codes
        or (not support_valid and facts.nearest_support is not None)
    )
    if hard_damage:
        reasons.append("STRUCTURAL_DAMAGE")

    valid = impulse_valid and retracement_valid and support_valid and vwap_valid and swing_valid
    score = 85
    if not impulse_valid:
        score -= 25
    if not retracement_valid:
        score -= 35
    if not support_valid:
        score -= 30
    if not vwap_valid:
        score -= 15
    if not swing_valid:
        score -= 20
    score = max(0, min(100, score))

    return StructuralIntegrityFacts(
        valid=valid and not hard_damage,
        score=score,
        swing_structure_valid=swing_valid,
        impulse_valid=impulse_valid,
        support_valid=support_valid,
        vwap_structure_valid=vwap_valid,
        retracement_valid=retracement_valid,
        hard_damage=hard_damage,
        reason_codes=reasons,
    )
