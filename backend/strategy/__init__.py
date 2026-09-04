"""Strategy promotion — Stage 8.

Immutable StrategyVersion rows + a linear promotion gate. Agents may propose
trades under a registered version; they may never promote one. Live and
autopilot require PRODUCTION. Paper confirmation may run on any non-rejected
registered version.
"""

from __future__ import annotations

from enum import StrEnum


class StrategyPromotionStage(StrEnum):
    PROPOSED = "proposed"
    BACKTEST_PASSED = "backtest_passed"
    OOS_PASSED = "oos_passed"
    WALK_FORWARD_PASSED = "walk_forward_passed"
    PAPER_PASSED = "paper_passed"
    HUMAN_APPROVED = "human_approved"
    PRODUCTION = "production"
    REJECTED = "rejected"


# Linear research → paper → human → production. REJECTED is off-axis.
PROMOTION_ORDER: tuple[StrategyPromotionStage, ...] = (
    StrategyPromotionStage.PROPOSED,
    StrategyPromotionStage.BACKTEST_PASSED,
    StrategyPromotionStage.OOS_PASSED,
    StrategyPromotionStage.WALK_FORWARD_PASSED,
    StrategyPromotionStage.PAPER_PASSED,
    StrategyPromotionStage.HUMAN_APPROVED,
    StrategyPromotionStage.PRODUCTION,
)


def stage_index(stage: StrategyPromotionStage | str) -> int:
    value = StrategyPromotionStage(stage)
    if value is StrategyPromotionStage.REJECTED:
        return -1
    return PROMOTION_ORDER.index(value)


def stage_at_least(
    current: StrategyPromotionStage | str,
    required: StrategyPromotionStage | str,
) -> bool:
    return stage_index(current) >= stage_index(required)
