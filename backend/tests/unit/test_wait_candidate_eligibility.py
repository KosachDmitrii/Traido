"""Stable WAIT eligibility covers geometry, not price-sensitive setup scores."""

from __future__ import annotations

from decimal import Decimal

from trading.wait_candidate import evaluate_wait_candidate_eligibility


def test_plan_below_absolute_rr_floor_is_not_actionable_wait() -> None:
    result = evaluate_wait_candidate_eligibility(
        entry=Decimal("220.199"),
        stop=Decimal("210.348"),
        target=Decimal("234.280"),
    )

    assert result.eligible is False
    assert result.planned_rr is not None and result.planned_rr < 1.45
    assert result.reason_codes == ("PLANNED_RR_BELOW_BASE_FLOOR",)


def test_candidate_floor_boundaries_are_actionable() -> None:
    result = evaluate_wait_candidate_eligibility(
        entry=Decimal(100),
        stop=Decimal(90),
        target=Decimal("114.5"),
    )

    assert result.eligible is True
    assert result.planned_rr == 1.45
    assert result.reason_codes == ()


def test_invalid_geometry_is_not_actionable_wait() -> None:
    result = evaluate_wait_candidate_eligibility(
        entry=Decimal(100),
        stop=Decimal(101),
        target=Decimal(110),
    )

    assert result.eligible is False
    assert result.planned_rr is None
    assert result.reason_codes == ("INVALID_WAIT_GEOMETRY",)
