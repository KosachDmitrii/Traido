"""ApprovalEvidence construction + sealed evaluate_final_approval."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from core.enums import AdmissionDecision, DataHealthStatus
from core.schemas import (
    AdmissionInput,
    ApprovalCommand,
    ApprovalEvidence,
    TradeAdmissionResult,
)
from trading.admission_records import build_request_fingerprint
from trading.approval_errors import DataBlockedError, NoTradeError
from trading.final_pretrade import require_final_admission
from trading.trade_admission import evaluate_from_admission_input


@dataclass(frozen=True)
class FinalApprovalResult:
    admission: TradeAdmissionResult
    evidence: ApprovalEvidence
    fingerprint: str


def _require_block(name: str, present: bool, missing: list[str]) -> None:
    if not present:
        missing.append(name)


def evaluate_final_approval(
    *,
    command: ApprovalCommand,
    admission_input: AdmissionInput,
    geometry_hash: str,
    sized_qty: Decimal,
    limit_price: Decimal,
    stop_price: Decimal,
    risk_verdict: str,
    liquidity_ok: bool,
    prior_admission: TradeAdmissionResult | None = None,
) -> FinalApprovalResult:
    """Sole capital-path gate that may mint BUY_ALLOWED ApprovalEvidence.

    When ``prior_admission`` is the result of final_pretrade / market+sector
    orchestration, it is sealed rather than re-derived from a partial helper
    that ignores market/sector blocks.
    """
    if command.user_decision.value != "approve":
        raise DataBlockedError("approval_command_not_approve")
    if (
        admission_input.opportunity_id is not None
        and command.opportunity_id != admission_input.opportunity_id
    ):
        raise DataBlockedError("opportunity_id_mismatch")
    if (
        admission_input.request_id is not None
        and command.request_id != admission_input.request_id
    ):
        raise DataBlockedError("request_id_mismatch")
    if command.expected_decision_version != admission_input.decision_version:
        from trading.approval_errors import StaleDecisionError

        raise StaleDecisionError(
            f"decision_version:{admission_input.decision_version}"
            f"!={command.expected_decision_version}"
        )

    sealed = admission_input.model_copy(
        update={
            "request_id": command.request_id,
            "decision_version": command.expected_decision_version,
            "sized_qty": sized_qty,
            "limit_price": limit_price,
            "stop_price": stop_price,
            "geometry_hash": geometry_hash,
            "portfolio_snapshot": dict(admission_input.portfolio_snapshot),
            "risk_snapshot": dict(admission_input.risk_snapshot),
            "liquidity_snapshot": dict(admission_input.liquidity_snapshot),
        }
    )

    missing: list[str] = []
    _require_block("risk_snapshot", bool(sealed.risk_snapshot), missing)
    _require_block("portfolio_snapshot", bool(sealed.portfolio_snapshot), missing)
    _require_block("liquidity_snapshot", bool(sealed.liquidity_snapshot), missing)
    _require_block("news_status", sealed.news_status is not None, missing)
    _require_block("earnings_status", sealed.earnings_status is not None, missing)
    _require_block(
        "sector",
        sealed.sector_tradable is not None and sealed.sector_source_ts is not None,
        missing,
    )
    _require_block("market", sealed.market is not None, missing)
    if missing:
        raise DataBlockedError(",".join(missing))

    if not liquidity_ok:
        raise NoTradeError("liquidity_failed")
    if risk_verdict.lower() not in {"pass", "ok", "approved"}:
        raise NoTradeError(f"risk_{risk_verdict}")

    if prior_admission is not None:
        admission = require_final_admission(prior_admission)
    else:
        admission = require_final_admission(evaluate_from_admission_input(sealed))
    if admission.decision is not AdmissionDecision.BUY_ALLOWED or not admission.admitted:
        raise NoTradeError(admission.decision.value)
    if admission.data_status is not DataHealthStatus.HEALTHY:
        raise DataBlockedError(f"data_status:{admission.data_status.value}")

    fp = build_request_fingerprint(
        sealed,
        geometry_hash=geometry_hash,
        decision_version=command.expected_decision_version,
        request_id=command.request_id,
        sized_qty=sized_qty,
        limit_price=limit_price,
    )
    evidence = _SealedApprovalEvidence(
        command=command,
        admission_input=sealed,
        geometry_hash=geometry_hash,
        decision_version=command.expected_decision_version,
        sized_qty=sized_qty,
        limit_price=limit_price,
        stop_price=stop_price,
        risk_verdict=risk_verdict,
        liquidity_ok=liquidity_ok,
        request_fingerprint=fp,
    )
    return FinalApprovalResult(admission=admission, evidence=evidence, fingerprint=fp)


class _SealedApprovalEvidence(ApprovalEvidence):
    """ApprovalEvidence that refuses authority-field model_copy updates."""

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False):
        if update:
            raise TypeError(
                "ApprovalEvidence is sealed; rebuild via evaluate_final_approval"
            )
        return super().model_copy(update=update, deep=deep)


def evidence_evaluated_at(evidence: ApprovalEvidence) -> datetime:
    return evidence.admission_input.evaluated_at


def evidence_request_id(evidence: ApprovalEvidence) -> UUID:
    return evidence.command.request_id
