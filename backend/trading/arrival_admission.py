"""Zone arrival gates — hard damage vs BUY confirmation.

Hard arrival (crash, gap-down, structural break, unread bars) is candidate
policy: the same at every slider, and it may refuse a WAIT. Soft arrival
(quality floors, sell-off, fast pullback) is BUY confirmation only.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading.entry_policy import EntryThresholds
from trading.zone_arrival import ZoneArrivalFacts

_HARD_ARRIVAL_TYPES = frozenset({"CRASH", "GAP_DOWN", "STRUCTURAL_BREAK"})


@dataclass(frozen=True)
class ArrivalGateResult:
    blocked: bool
    hard_veto: bool
    reason_codes: list[str]
    warnings: list[str]
    veto_codes: list[str]

    def desk_summary(self) -> str | None:
        if not self.reason_codes and not self.warnings:
            return None
        parts = [*self.reason_codes[:3], *[f"warn:{w}" for w in self.warnings[:2]]]
        return " · ".join(parts)


def evaluate_hard_arrival(
    arrival: ZoneArrivalFacts,
    *,
    structural_hard: bool = True,
) -> ArrivalGateResult:
    """Candidate-layer arrival. Slider-invariant. Damage and unread bars only."""
    warnings: list[str] = []

    if arrival.crash_velocity:
        return ArrivalGateResult(
            blocked=True,
            hard_veto=True,
            reason_codes=["CRASH_VELOCITY", *arrival.reason_codes[:3]],
            warnings=[],
            veto_codes=["CRASH_VELOCITY"],
        )

    if arrival.structural_damage and structural_hard:
        return ArrivalGateResult(
            blocked=True,
            hard_veto=True,
            reason_codes=["STRUCTURAL_DAMAGE", *arrival.reason_codes[:3]],
            warnings=[],
            veto_codes=["STRUCTURAL_DAMAGE"],
        )
    if arrival.structural_damage:
        warnings.append("STRUCTURAL_DAMAGE_SOFT")

    t = arrival.arrival_type.value
    if t in _HARD_ARRIVAL_TYPES:
        code = f"ARRIVAL_TYPE_{t}"
        return ArrivalGateResult(
            blocked=True,
            hard_veto=True,
            reason_codes=[code],
            warnings=warnings,
            veto_codes=[code],
        )

    if "INSUFFICIENT_BARS" in arrival.reason_codes:
        return ArrivalGateResult(
            blocked=True,
            hard_veto=True,
            reason_codes=["INSUFFICIENT_BARS", "DATA_BLOCKED"],
            warnings=warnings,
            veto_codes=["INSUFFICIENT_BARS"],
        )

    return ArrivalGateResult(
        blocked=False,
        hard_veto=False,
        reason_codes=[],
        warnings=warnings,
        veto_codes=[],
    )


def evaluate_soft_arrival(
    arrival: ZoneArrivalFacts,
    *,
    min_zone_arrival_quality: int,
    allow_fast_pullback: bool,
    allow_sell_off_arrival: bool,
    min_sell_off_arrival_quality: int,
    min_fast_pullback_arrival_quality: int,
) -> ArrivalGateResult:
    """BUY-confirmation arrival. Quality floors follow the slider."""
    t = arrival.arrival_type.value
    if t == "SELL_OFF" and not allow_sell_off_arrival:
        return ArrivalGateResult(
            blocked=True,
            hard_veto=False,
            reason_codes=["ARRIVAL_TYPE_SELL_OFF"],
            warnings=[],
            veto_codes=[],
        )

    floor = min_zone_arrival_quality
    if allow_sell_off_arrival and t == "SELL_OFF":
        floor = min_sell_off_arrival_quality
    elif allow_fast_pullback and t == "FAST_PULLBACK":
        floor = min_fast_pullback_arrival_quality

    if arrival.score < floor:
        return ArrivalGateResult(
            blocked=True,
            hard_veto=False,
            reason_codes=[f"ZONE_ARRIVAL_QUALITY_LOW:{int(arrival.score)}<{floor}"],
            warnings=[],
            veto_codes=[],
        )

    warnings: list[str] = []
    if t == "SELL_OFF" and allow_sell_off_arrival:
        warnings.append("SELL_OFF_CAUTION")
    return ArrivalGateResult(
        blocked=False,
        hard_veto=False,
        reason_codes=[],
        warnings=warnings,
        veto_codes=[],
    )


def evaluate_arrival_gate(arrival: ZoneArrivalFacts, th: EntryThresholds) -> ArrivalGateResult:
    """BUY-facing compose: hard damage first, then slider confirmation."""
    hard = evaluate_hard_arrival(arrival, structural_hard=th.structural_arrival_hard)
    if hard.blocked:
        return hard
    soft = evaluate_soft_arrival(
        arrival,
        min_zone_arrival_quality=th.min_zone_arrival_quality,
        allow_fast_pullback=th.allow_fast_pullback,
        allow_sell_off_arrival=th.allow_sell_off_arrival,
        min_sell_off_arrival_quality=th.min_sell_off_arrival_quality,
        min_fast_pullback_arrival_quality=th.min_fast_pullback_arrival_quality,
    )
    if not hard.warnings:
        return soft
    return ArrivalGateResult(
        blocked=soft.blocked,
        hard_veto=soft.hard_veto,
        reason_codes=soft.reason_codes,
        warnings=[*hard.warnings, *soft.warnings],
        veto_codes=soft.veto_codes,
    )


def buy_blocked_for_arrival(arrival: ZoneArrivalFacts, th: EntryThresholds) -> bool:
    return evaluate_arrival_gate(arrival, th).blocked
