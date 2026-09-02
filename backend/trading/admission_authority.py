"""Gate: persisted ApprovalAdmission is the sole authority for broker entry."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from core.enums import AdmissionDecision, DataHealthStatus, IntentPurpose
from core.metrics import METRICS
from core.schemas import AdmissionRecord, TradeOpportunity
from trading.order_intent import OrderIntent


class AdmissionAuthorityError(RuntimeError):
    """Raised when entry would contact the broker without valid ApprovalAdmission."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail or code
        super().__init__(f"{code}:{self.detail}")


def load_valid_approval_admission(
    record_id: UUID,
    *,
    opportunity: TradeOpportunity,
    geometry_hash: str | None,
    now: datetime | None = None,
) -> AdmissionRecord:
    """Load and verify an ApprovalAdmission record for capital-path entry."""
    # Import at call time so test monkeypatches of ADMISSION_RECORDS apply.
    from trading.admission_records import ADMISSION_RECORDS

    now = now or datetime.now(UTC)
    record = ADMISSION_RECORDS.get(record_id)
    if record is None:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "approval_record_missing")

    effective_phase = record.phase or record.context.get("phase")
    if effective_phase not in {None, "approval"}:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", f"wrong_phase:{effective_phase}")

    if record.opportunity_id is not None and record.opportunity_id != opportunity.id:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "opportunity_mismatch")

    if record.symbol.upper() != opportunity.candidate.symbol.upper():
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "symbol_mismatch")

    if (
        record.decision is not AdmissionDecision.BUY_ALLOWED
        or not record.admitted
        or record.data_status is DataHealthStatus.UNHEALTHY
    ):
        raise AdmissionAuthorityError(
            "BUY_REJECTED_ADMISSION",
            ",".join(record.reason_codes[:8]) or record.decision.value,
        )

    if record.expires_at is not None:
        exp = record.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        if exp <= now:
            raise AdmissionAuthorityError("ADMISSION_REQUIRED", "admission_expired")

    expected_gh = geometry_hash or opportunity.geometry_hash
    if expected_gh and record.geometry_hash and record.geometry_hash != expected_gh:
        METRICS.counter(
            "geometry_mismatch",
            help_text="ApprovalAdmission geometry_hash mismatch vs OrderIntent/Opportunity",
        )
        raise AdmissionAuthorityError("GEOMETRY_MISMATCH", record.geometry_hash)

    return record


def assert_entry_intent_has_admission(intent: OrderIntent, opportunity: TradeOpportunity) -> None:
    """Hard gate before place_order — EntryDecision/Risk PASS are not authority."""
    if intent.purpose is not IntentPurpose.ENTRY:
        return
    if intent.approval_admission_record_id is None:
        METRICS.counter(
            "entry_intent_without_admission",
            help_text="Entry OrderIntent missing ApprovalAdmission FK at broker gate",
        )
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "intent_missing_admission_fk")
    load_valid_approval_admission(
        intent.approval_admission_record_id,
        opportunity=opportunity,
        geometry_hash=intent.geometry_hash,
    )
