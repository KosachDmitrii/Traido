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


def _phase_of(record: AdmissionRecord) -> str | None:
    return record.phase or (
        str(record.context.get("phase")) if record.context.get("phase") else None
    )


def assert_authority_invariant(
    record: AdmissionRecord,
    opportunity: TradeOpportunity,
    intent: OrderIntent,
    *,
    now: datetime | None = None,
) -> AdmissionRecord:
    """Strict chain: record ↔ opportunity ↔ intent. Any NULL/mismatch refuses broker."""
    now = now or datetime.now(UTC)

    phase = _phase_of(record)
    if phase != "approval":
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", f"wrong_phase:{phase}")

    if record.opportunity_id is None:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "opportunity_id_null")
    if record.opportunity_id != opportunity.id:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "opportunity_mismatch")
    if intent.opportunity_id is None or intent.opportunity_id != opportunity.id:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "intent_opportunity_mismatch")

    if opportunity.approval_admission_record_id is None:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "opportunity_missing_approval_fk")
    if intent.approval_admission_record_id is None:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "intent_missing_admission_fk")
    if record.id != opportunity.approval_admission_record_id:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "opportunity_fk_mismatch")
    if record.id != intent.approval_admission_record_id:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "intent_fk_mismatch")

    if not record.geometry_hash:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "record_geometry_hash_null")
    if not opportunity.geometry_hash:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "opportunity_geometry_hash_null")
    if not intent.geometry_hash:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "intent_geometry_hash_null")
    if not (record.geometry_hash == opportunity.geometry_hash == intent.geometry_hash):
        METRICS.counter(
            "geometry_mismatch",
            help_text="ApprovalAdmission geometry_hash mismatch vs OrderIntent/Opportunity",
        )
        raise AdmissionAuthorityError("GEOMETRY_MISMATCH", record.geometry_hash)

    if record.symbol.upper() != opportunity.candidate.symbol.upper():
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "symbol_mismatch")
    if intent.symbol.upper() != opportunity.candidate.symbol.upper():
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "intent_symbol_mismatch")

    if (
        record.decision is not AdmissionDecision.BUY_ALLOWED
        or not record.admitted
        or record.data_status is not DataHealthStatus.HEALTHY
    ):
        raise AdmissionAuthorityError(
            "BUY_REJECTED_ADMISSION",
            ",".join(record.reason_codes[:8]) or record.decision.value,
        )

    if record.expires_at is None:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "expires_at_null")
    exp = record.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=UTC)
    if exp <= now:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "admission_expired")

    return record


def load_valid_approval_admission(
    record_id: UUID,
    *,
    opportunity: TradeOpportunity,
    intent: OrderIntent,
    now: datetime | None = None,
) -> AdmissionRecord:
    """Load and verify an ApprovalAdmission record for capital-path entry."""
    from trading.admission_records import ADMISSION_RECORDS

    record = ADMISSION_RECORDS.get(record_id)
    if record is None:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "approval_record_missing")
    return assert_authority_invariant(record, opportunity, intent, now=now)


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
        intent=intent,
    )
