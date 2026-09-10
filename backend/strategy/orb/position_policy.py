"""Read-only projection of immutable retest geometry; never invent a target."""

from decimal import Decimal, InvalidOperation


def observed_target(payload: dict) -> Decimal | None:
    plan = payload.get("orb_plan") or {}
    if plan.get("version") != "orb@2.0.0":
        return None
    retest = (plan.get("evidence") or {}).get("retest") or {}
    try:
        target = Decimal(str(retest["target"]))
        ceiling = Decimal(str(plan["max_entry"]))
        if (
            retest.get("phase") == "ready"
            and target.is_finite()
            and ceiling.is_finite()
            and target > ceiling > 0
        ):
            return target
    except (KeyError, ValueError, TypeError, InvalidOperation):
        pass
    return None
