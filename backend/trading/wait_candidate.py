"""Stable geometry eligibility for a WAIT plan.

Setup and entry quality contain price-relative inputs and must be recalculated
when a watch reaches its zone.  Only facts that waiting cannot repair belong
here: invalid entry/stop/target geometry and an absolute gross R:R failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading.buy_confirmation import BASE_RR_FLOOR
from trading.effective_rr import planned_long_rr


@dataclass(frozen=True)
class WaitCandidateEligibility:
    eligible: bool
    planned_rr: float | None
    reason_codes: tuple[str, ...]


def evaluate_wait_candidate_eligibility(
    *,
    entry: Decimal | float | None,
    stop: Decimal | float | None,
    target: Decimal | float | None,
) -> WaitCandidateEligibility:
    """Evaluate plan geometry that cannot be repaired by a zone touch."""
    reasons: list[str] = []
    rr = (
        planned_long_rr(entry, stop, target)
        if entry is not None and stop is not None and target is not None
        else None
    )
    if rr is None:
        reasons.append("INVALID_WAIT_GEOMETRY")
    elif rr < BASE_RR_FLOOR:
        reasons.append("PLANNED_RR_BELOW_BASE_FLOOR")

    return WaitCandidateEligibility(
        eligible=not reasons,
        planned_rr=rr,
        reason_codes=tuple(reasons),
    )
