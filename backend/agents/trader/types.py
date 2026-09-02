"""Shared types for the professional-trader desk chain.

Each agent owns one step. A fail stops the chain; nothing later runs.
Agents never place orders — they only pass/fail and emit reasons.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from core.enums import Timeframe
from core.schemas import FeatureSnapshot, MarketAssessment, NewsAssessment, TechnicalAssessment


class TraderStep(StrEnum):
    CONTEXT = "context"
    UNIVERSE = "universe"
    STRUCTURE = "structure"
    SETUP = "setup"
    ENTRY = "entry"
    RISK_PLAN = "risk_plan"
    CHECKLIST = "checklist"


@dataclass
class StepResult:
    step: TraderStep
    ok: bool
    detail: str
    reasons: list[str] = field(default_factory=list)
    score: int | None = None


@dataclass
class RiskPlan:
    entry: Decimal
    stop: Decimal
    target: Decimal
    risk_reward: float
    exec_timeframe: Timeframe
    reasons: list[str] = field(default_factory=list)


@dataclass
class TraderBundle:
    """Accumulated facts while the chain runs. Downstream agents read only this."""

    symbol: str
    features: dict[Timeframe, FeatureSnapshot] = field(default_factory=dict)
    market: MarketAssessment | None = None
    news: NewsAssessment | None = None
    technical: TechnicalAssessment | None = None
    risk_plan: RiskPlan | None = None
    steps: list[StepResult] = field(default_factory=list)
    quote_spread_bps: float | None = None
    last_price: Decimal | None = None

    def record(self, result: StepResult) -> None:
        self.steps.append(result)

    @property
    def failed(self) -> StepResult | None:
        for step in self.steps:
            if not step.ok:
                return step
        return None
