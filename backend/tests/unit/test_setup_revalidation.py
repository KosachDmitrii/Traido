"""Triggered watches refresh dynamic setup evidence without losing catalyst context."""

from __future__ import annotations

from core.schemas import SetupQualityBreakdown
from trading.setup_revalidation import merge_revalidated_setup


def _breakdown(**overrides: int) -> SetupQualityBreakdown:
    values = {
        "trend_structure": 60,
        "impulse_quality": 60,
        "retracement_structure": 60,
        "volume_participation": 60,
        "support_structure": 60,
        "market_alignment": 60,
        "catalyst": 60,
        "liquidity": 60,
    }
    values.update(overrides)
    return SetupQualityBreakdown(**values)


def test_dynamic_components_are_taken_from_fresh_revalidation() -> None:
    fresh = _breakdown(retracement_structure=92, support_structure=80, catalyst=70)
    persisted = _breakdown(retracement_structure=15, support_structure=35, catalyst=84).as_dict()

    merged = merge_revalidated_setup(fresh, persisted)

    assert merged.retracement_structure == 92
    assert merged.support_structure == 80
    assert merged.catalyst == 84
    assert merged.total != _breakdown(**persisted).total


def test_missing_persisted_catalyst_uses_fresh_evidence() -> None:
    fresh = _breakdown(catalyst=70)

    assert merge_revalidated_setup(fresh, {}) == fresh
