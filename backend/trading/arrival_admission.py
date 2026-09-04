"""Zone arrival gates — shared by TradeAdmission and desk display."""

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


def _effective_min_arrival(arrival: ZoneArrivalFacts, th: EntryThresholds) -> int:
    t = arrival.arrival_type.value
    if th.allow_sell_off_arrival and t == "SELL_OFF":
        return th.min_sell_off_arrival_quality
    if th.allow_fast_pullback and t == "FAST_PULLBACK":
        return th.min_fast_pullback_arrival_quality
    return th.min_zone_arrival_quality


def evaluate_arrival_gate(arrival: ZoneArrivalFacts, th: EntryThresholds) -> ArrivalGateResult:
    """Arrival confirmation — quality floors follow the slider; damage stays hard."""
    warnings: list[str] = []

    if arrival.crash_velocity:
        return ArrivalGateResult(
            blocked=True,
            hard_veto=True,
            reason_codes=["CRASH_VELOCITY", *arrival.reason_codes[:3]],
            warnings=[],
            veto_codes=["CRASH_VELOCITY"],
        )

    if arrival.structural_damage and th.structural_arrival_hard:
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

    if t == "SELL_OFF" and not th.allow_sell_off_arrival:
        return ArrivalGateResult(
            blocked=True,
            hard_veto=False,
            reason_codes=["ARRIVAL_TYPE_SELL_OFF"],
            warnings=warnings,
            veto_codes=[],
        )

    if "INSUFFICIENT_BARS" in arrival.reason_codes:
        return ArrivalGateResult(
            blocked=True,
            hard_veto=True,
            reason_codes=["INSUFFICIENT_BARS", "DATA_BLOCKED"],
            warnings=warnings,
            veto_codes=["INSUFFICIENT_BARS"],
        )

    floor = _effective_min_arrival(arrival, th)
    if arrival.score < floor:
        return ArrivalGateResult(
            blocked=True,
            hard_veto=False,
            reason_codes=[f"ZONE_ARRIVAL_QUALITY_LOW:{int(arrival.score)}<{floor}"],
            warnings=warnings,
            veto_codes=[],
        )

    if t == "SELL_OFF" and th.allow_sell_off_arrival:
        warnings.append("SELL_OFF_CAUTION")

    return ArrivalGateResult(
        blocked=False,
        hard_veto=False,
        reason_codes=[],
        warnings=warnings,
        veto_codes=[],
    )


def buy_blocked_for_arrival(arrival: ZoneArrivalFacts, th: EntryThresholds) -> bool:
    return evaluate_arrival_gate(arrival, th).blocked
