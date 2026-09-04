"""Paper-only admission quality floors and controlled setup compensation.

Single source of truth for the paper relaxation table. LIVE keeps the
historical floors in ``entry_policy.thresholds_for``. Compensation never
mints BUY_ALLOWED — it only lets a borderline setup continue the existing
admission pipeline.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from typing import Any

from core.enums import AdmissionDecision, BrokerEnvironment
from core.metrics import METRICS
from core.schemas import TradeAdmissionResult

logger = logging.getLogger(__name__)

# Compensation may close at most this many setup points below the paper floor.
MAX_SETUP_DEFICIT = 3
# Planned long R:R required to compensate a setup deficit. Same formula as
# ``effective_rr.planned_long_rr`` — not a second relaxation-only R:R.
COMPENSATION_MIN_RR = 2.0

FUNNEL_GATES: tuple[str, ...] = (
    "candidates_seen",
    "in_entry_zone",
    "setup_below_floor",
    "setup_compensated",
    "entry_rejected",
    "rr_rejected",
    "regime_rejected",
    "risk_rejected",
    "data_blocked",
    "reached_admission",
    "buy_allowed",
    "orders_created",
    "orders_filled",
)


@dataclass(frozen=True)
class PaperQualityFloors:
    setup_floor: int
    entry_floor: int
    weak_setup_min_rr: float


# Canonical paper quality floors — do not copy these numbers elsewhere.
PAPER_QUALITY_FLOORS: dict[int, PaperQualityFloors] = {
    0: PaperQualityFloors(58, 53, 2.30),
    25: PaperQualityFloors(56, 51, 2.10),
    50: PaperQualityFloors(53, 48, 1.90),
    75: PaperQualityFloors(50, 46, 1.70),
    100: PaperQualityFloors(47, 44, 1.45),
}

_PAPER_STEPS: tuple[int, ...] = tuple(PAPER_QUALITY_FLOORS)


@dataclass(frozen=True)
class SetupCompensationResult:
    eligible: bool
    applied: bool
    setup_deficit: float
    deny_reason: str | None = None


def is_paper_broker() -> bool:
    from core.config import get_settings

    return get_settings().broker_env is BrokerEnvironment.PAPER


def snap_relaxation_level(level: float) -> int:
    raw = int(level)
    return min(_PAPER_STEPS, key=lambda step: abs(step - raw))


def paper_quality_floors(level: float) -> PaperQualityFloors:
    return PAPER_QUALITY_FLOORS[snap_relaxation_level(level)]


def evaluate_setup_compensation(
    *,
    paper: bool,
    setup_score: float,
    setup_floor: float,
    entry_score: float,
    entry_floor: float,
    price_in_entry_zone: bool,
    rr: float | None,
    regime_allowed: bool,
    required_market_data_fresh: bool,
    hard_risk_block: bool,
    broker_or_data_block: bool,
) -> SetupCompensationResult:
    """Allow a 1–3 point paper setup miss to continue admission, not to buy."""
    setup_deficit = float(setup_floor) - float(setup_score)
    if setup_deficit <= 0:
        return SetupCompensationResult(
            eligible=False,
            applied=False,
            setup_deficit=setup_deficit,
        )
    if not paper:
        return SetupCompensationResult(
            eligible=False,
            applied=False,
            setup_deficit=setup_deficit,
        )
    if setup_deficit > MAX_SETUP_DEFICIT:
        return SetupCompensationResult(
            eligible=False,
            applied=False,
            setup_deficit=setup_deficit,
        )
    if entry_score < entry_floor:
        return SetupCompensationResult(
            eligible=False,
            applied=False,
            setup_deficit=setup_deficit,
            deny_reason="ENTRY_BELOW_FLOOR",
        )
    if not price_in_entry_zone:
        return SetupCompensationResult(
            eligible=False,
            applied=False,
            setup_deficit=setup_deficit,
        )
    if rr is None or not math.isfinite(rr) or rr < COMPENSATION_MIN_RR:
        return SetupCompensationResult(
            eligible=False,
            applied=False,
            setup_deficit=setup_deficit,
            deny_reason="RR_BELOW_COMPENSATION_FLOOR",
        )
    if not regime_allowed:
        return SetupCompensationResult(
            eligible=False,
            applied=False,
            setup_deficit=setup_deficit,
        )
    if not required_market_data_fresh or broker_or_data_block:
        return SetupCompensationResult(
            eligible=False,
            applied=False,
            setup_deficit=setup_deficit,
        )
    if hard_risk_block:
        return SetupCompensationResult(
            eligible=False,
            applied=False,
            setup_deficit=setup_deficit,
        )
    return SetupCompensationResult(
        eligible=True,
        applied=True,
        setup_deficit=setup_deficit,
    )


def record_funnel(gate: str) -> None:
    if gate not in FUNNEL_GATES:
        return
    METRICS.counter(
        "admission_funnel",
        labels={"gate": gate},
        help_text="Paper admission relaxation funnel",
    )


def final_reason_code(result: TradeAdmissionResult) -> str:
    """Gate that stopped the candidate after compensation, if any."""
    decision = result.decision
    codes = list(result.reason_codes)
    if decision is AdmissionDecision.BUY_ALLOWED:
        return "BUY_ALLOWED"
    if decision is AdmissionDecision.DATA_BLOCKED:
        for code in codes:
            if code != "DATA_BLOCKED":
                return code
        return "DATA_BLOCKED"
    rest = [code for code in codes if code != "SETUP_COMPENSATED"]
    for preferred in (
        "WAITING_CONFIRMATION",
        "SETUP_BELOW_FLOOR",
        "ENTRY_BELOW_FLOOR",
        "RR_BELOW_COMPENSATION_FLOOR",
    ):
        if preferred in rest:
            return preferred
    if rest:
        return rest[-1]
    if decision is AdmissionDecision.WAIT:
        return "WAITING_CONFIRMATION"
    return decision.value.upper()


def emit_relaxation_observation(
    *,
    symbol: str,
    relaxation_level: int,
    setup_score: float,
    setup_floor: float,
    entry_score: float,
    entry_floor: float,
    price_in_entry_zone: bool,
    rr: float | None,
    compensation_eligible: bool,
    compensation_applied: bool,
    result: TradeAdmissionResult,
    regime_allowed: bool = True,
    hard_risk_block: bool = False,
    reached_admission: bool = False,
) -> None:
    setup_deficit = float(setup_floor) - float(setup_score)
    payload: dict[str, Any] = {
        "symbol": symbol,
        "relaxation_level": relaxation_level,
        "setup_score": setup_score,
        "setup_floor": setup_floor,
        "setup_deficit": setup_deficit,
        "entry_score": entry_score,
        "entry_floor": entry_floor,
        "price_in_entry_zone": price_in_entry_zone,
        "rr": None if rr is None else round(rr, 4),
        "compensation_eligible": compensation_eligible,
        "compensation_applied": compensation_applied,
        "final_decision": result.decision.value.upper(),
        "final_reason": final_reason_code(result),
    }
    logger.info("admission_relaxation %s", json.dumps(payload, default=str))

    record_funnel("candidates_seen")
    if price_in_entry_zone:
        record_funnel("in_entry_zone")
    if setup_score < setup_floor:
        record_funnel("setup_below_floor")
    if compensation_applied:
        record_funnel("setup_compensated")
    if entry_score < entry_floor:
        record_funnel("entry_rejected")
    if (
        "INSUFFICIENT_EFFECTIVE_RR" in result.reason_codes
        or any(c.startswith("INSUFFICIENT_EFFECTIVE_RR:") for c in result.reason_codes)
        or "RR_BELOW_COMPENSATION_FLOOR" in result.reason_codes
    ):
        record_funnel("rr_rejected")
    if not regime_allowed or any(c.startswith("REGIME_") for c in result.reason_codes):
        record_funnel("regime_rejected")
    if hard_risk_block or any(
        c
        in {
            "INVALID_STOP",
            "TARGET_UNREALISTIC",
            "STRUCTURAL_DAMAGE",
            "MISSING_STOP",
            "MISSING_TARGET",
        }
        for c in result.reason_codes + result.vetoes
    ):
        record_funnel("risk_rejected")
    if result.decision is AdmissionDecision.DATA_BLOCKED:
        record_funnel("data_blocked")
    if reached_admission:
        record_funnel("reached_admission")
    if result.decision is AdmissionDecision.BUY_ALLOWED:
        record_funnel("buy_allowed")
