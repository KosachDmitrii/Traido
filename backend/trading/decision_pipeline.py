"""
Canonical DecisionPipeline for NEW EXPOSURE.

Gate order is declared here so it cannot emerge from a 400-line `decide()` by
accident. Each gate returns PASS / FAIL / UNAVAILABLE. Mandatory UNAVAILABLE
fails closed — absence of a fact is not clearance.

This module does not call the broker. It only answers: may this candidate become
an ExecutableDecision? Placement remains `ExecutionService`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class GateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class GateResult:
    gate: str
    status: GateStatus
    reason_code: str = ""
    measured_value: str | None = None
    threshold: str | None = None
    provider: str | None = None
    event_time: datetime | None = None
    received_at: datetime | None = None
    age_sec: float | None = None
    quality: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is GateStatus.PASS

    def as_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "status": self.status.value,
            "reason_code": self.reason_code,
            "measured_value": self.measured_value,
            "threshold": self.threshold,
            "provider": self.provider,
            "event_time": self.event_time.isoformat() if self.event_time else None,
            "received_at": self.received_at.isoformat() if self.received_at else None,
            "age_sec": self.age_sec,
            "quality": self.quality,
            **self.detail,
        }


# Declared order for increasing exposure. Reordering is a product decision,
# not an implementation convenience — change the list and the tests together.
NEW_EXPOSURE_GATE_ORDER: tuple[str, ...] = (
    "kill_switch",
    "data_configuration",
    "data_freshness",
    "instrument_eligibility",
    "broker_health",
    "reconciliation_freshness",
    "market_hours",
    "corporate_action",
    "event_risk",
    "liquidity",
    "portfolio_exposure",
    "risk_engine",
)


@dataclass(frozen=True)
class ExecutableDecision:
    """Facts the execution path may act on after every mandatory gate PASSed."""

    symbol: str
    qty: Decimal
    limit_price: Decimal
    stop_price: Decimal
    proposed_qty: Decimal | None = None
    gate_results: tuple[GateResult, ...] = ()


@dataclass(frozen=True)
class PipelineRefusal:
    failed: GateResult
    gate_results: tuple[GateResult, ...]


def first_refusal(results: list[GateResult]) -> PipelineRefusal | None:
    for result in results:
        if result.status is GateStatus.PASS:
            continue
        # UNAVAILABLE on a mandatory gate is FAIL CLOSED.
        return PipelineRefusal(failed=result, gate_results=tuple(results))
    return None


def pass_gate(name: str, **detail: Any) -> GateResult:
    return GateResult(gate=name, status=GateStatus.PASS, detail=dict(detail))


def fail_gate(name: str, reason_code: str, **detail: Any) -> GateResult:
    return GateResult(
        gate=name, status=GateStatus.FAIL, reason_code=reason_code, detail=dict(detail)
    )


def unavailable_gate(name: str, reason_code: str, **detail: Any) -> GateResult:
    return GateResult(
        gate=name,
        status=GateStatus.UNAVAILABLE,
        reason_code=reason_code,
        detail=dict(detail),
    )
