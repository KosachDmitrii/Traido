"""Stable eligibility for a WAIT plan.

WAIT is reserved for a valid trade idea whose *transient* entry conditions
are not ready yet.  A setup below the candidate floor or geometry whose gross
R:R cannot clear the absolute BUY floor cannot improve merely by touching the
zone, so neither belongs in the watch state machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading.buy_confirmation import BASE_RR_FLOOR, CANDIDATE_SETUP_FLOOR
from trading.effective_rr import planned_long_rr


@dataclass(frozen=True)
class WaitCandidateEligibility:
    eligible: bool
    planned_rr: float | None
    reason_codes: tuple[str, ...]


def evaluate_wait_candidate_eligibility(
    *,
    setup_quality: int,
    entry: Decimal | float | None,
    stop: Decimal | float | None,
    target: Decimal | float | None,
) -> WaitCandidateEligibility:
    """Evaluate facts that cannot be repaired by waiting for a zone touch."""
    reasons: list[str] = []
    if setup_quality < CANDIDATE_SETUP_FLOOR:
        reasons.append("CANDIDATE_SETUP_BELOW_FLOOR")

    rr = (
        planned_long_rr(entry, stop, target)
        if entry is not None and stop is not None and target is not None
        else None
    )
    if rr is None or rr < BASE_RR_FLOOR:
        reasons.append("PLANNED_RR_BELOW_BASE_FLOOR")

    return WaitCandidateEligibility(
        eligible=not reasons,
        planned_rr=rr,
        reason_codes=tuple(reasons),
    )
