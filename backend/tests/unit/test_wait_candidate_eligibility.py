"""Stable WAIT eligibility must match the absolute BUY candidate floors."""

from __future__ import annotations

from decimal import Decimal

from trading.wait_candidate import evaluate_wait_candidate_eligibility


def test_setup_below_candidate_floor_is_not_actionable_wait() -> None:
    result = evaluate_wait_candidate_eligibility(
        setup_quality=54,
        entry=Decimal(100),
        stop=Decimal(95),
        target=Decimal(110),
    )

    assert result.eligible is False
    assert result.reason_codes == ("CANDIDATE_SETUP_BELOW_FLOOR",)


def test_plan_below_absolute_rr_floor_is_not_actionable_wait() -> None:
    result = evaluate_wait_candidate_eligibility(
        setup_quality=70,
        entry=Decimal("220.199"),
        stop=Decimal("210.348"),
        target=Decimal("234.280"),
    )

    assert result.eligible is False
    assert result.planned_rr is not None and result.planned_rr < 1.45
    assert result.reason_codes == ("PLANNED_RR_BELOW_BASE_FLOOR",)


def test_candidate_floor_boundaries_are_actionable() -> None:
    result = evaluate_wait_candidate_eligibility(
        setup_quality=55,
        entry=Decimal(100),
        stop=Decimal(90),
        target=Decimal("114.5"),
    )

    assert result.eligible is True
    assert result.planned_rr == 1.45
    assert result.reason_codes == ()
