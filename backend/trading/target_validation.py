"""Target must have structural basis — not inflated for R:R."""

from __future__ import annotations

from decimal import Decimal

from core.enums import TargetReachabilityClass
from core.schemas import TargetPlan, TargetValidationResult

VALID_BASES = frozenset({"2R", "atr", "structure", "historical_mfe"})
# Absolute price tolerance for candidate.target ≈ target_plan.price (tick-aware).
DEFAULT_TICK = 0.01
TICK_TOLERANCE_MULT = 2.0


def validate_target(
    *,
    entry: Decimal | float,
    target: Decimal | float,
    target_plan: TargetPlan | None = None,
    tick_size: float = DEFAULT_TICK,
) -> TargetValidationResult:
    entry_f = float(entry)
    target_f = float(target)
    reasons: list[str] = []

    if target_f <= entry_f:
        return TargetValidationResult(
            valid=False,
            basis=None,
            reason_codes=["MISSING_TARGET"],
        )

    if target_plan is None:
        return TargetValidationResult(
            valid=False,
            basis=None,
            reason_codes=["MISSING_TARGET"],
        )

    basis = target_plan.model
    if basis not in VALID_BASES:
        reasons.append("TARGET_NO_BASIS")

    if target_plan.reachability is TargetReachabilityClass.UNREALISTIC:
        reasons.append("TARGET_UNREALISTIC")

    plan_price = float(target_plan.price)
    tol = max(tick_size * TICK_TOLERANCE_MULT, abs(plan_price) * 1e-6)
    if abs(target_f - plan_price) > tol:
        reasons.append("TARGET_PLAN_MISMATCH")

    valid = (
        basis in VALID_BASES
        and target_plan.reachability not in {TargetReachabilityClass.UNREALISTIC}
        and "TARGET_PLAN_MISMATCH" not in reasons
    )
    if not valid and "TARGET_UNREALISTIC" not in reasons and "TARGET_PLAN_MISMATCH" not in reasons:
        reasons.append("TARGET_UNREALISTIC")

    return TargetValidationResult(
        valid=valid,
        basis=basis,
        reason_codes=reasons,
    )
