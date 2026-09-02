"""Gate: persisted ApprovalAdmission is the sole authority for broker entry."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from core.enums import AdmissionDecision, DataHealthStatus, IntentPurpose
from core.metrics import METRICS
from core.schemas import AdmissionRecord, TradeOpportunity
from trading.order_intent import OrderIntent
from trading.pricing import round_equity_price, round_equity_qty


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


def _record_request_id(record: AdmissionRecord) -> UUID | None:
    if record.request_id is not None:
        return record.request_id
    raw = record.context.get("request_id")
    if not raw:
        return None
    try:
        return UUID(str(raw))
    except ValueError:
        return None


def _context_admission_input(record: AdmissionRecord) -> dict[str, object]:
    ctx = record.context or {}
    adm = ctx.get("admission_input")
    if isinstance(adm, dict):
        return adm
    if isinstance(record.admission_input, dict):
        return record.admission_input
    return {}


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def assert_entry_broker_authority(
    intent: OrderIntent,
    opportunity: TradeOpportunity,
    record: AdmissionRecord,
    *,
    broker_environment: str | None = None,
) -> None:
    """Verify sealed approval evidence and broker identity for entry intents."""
    if intent.purpose is not IntentPurpose.ENTRY:
        return

    if not intent.broker_account_id:
        METRICS.counter(
            "broker_authority_rejected",
            help_text="Entry intent missing broker_account_id at authority gate",
        )
        raise AdmissionAuthorityError("BROKER_ACCOUNT_IDENTITY_REQUIRED", "broker_account_id_null")

    env = (intent.broker_environment or broker_environment or "paper").strip().lower()
    if env != "paper":
        METRICS.counter(
            "broker_environment_blocked",
            help_text="Entry intent broker environment is not paper",
        )
        raise AdmissionAuthorityError("BROKER_ENVIRONMENT_BLOCKED", env)

    rec_fp = record.request_fingerprint or record.context.get("request_fingerprint")
    if rec_fp and intent.request_fingerprint and str(rec_fp) != intent.request_fingerprint:
        METRICS.counter(
            "approval_fingerprint_mismatch",
            help_text="ApprovalAdmission fingerprint mismatch vs OrderIntent",
        )
        raise AdmissionAuthorityError("APPROVAL_FINGERPRINT_MISMATCH", str(rec_fp))

    rec_rid = _record_request_id(record)
    if intent.request_id is not None and rec_rid is not None and intent.request_id != rec_rid:
        METRICS.counter(
            "broker_authority_rejected",
            help_text="ApprovalAdmission request_id mismatch vs OrderIntent",
        )
        raise AdmissionAuthorityError("REQUEST_ID_MISMATCH", str(rec_rid))

    adm_inp = _context_admission_input(record)
    sized = _decimal_or_none(adm_inp.get("sized_qty"))
    if sized is not None and intent.requested_qty != round_equity_qty(sized):
        METRICS.counter(
            "broker_authority_rejected",
            help_text="Entry intent qty mismatch vs ApprovalAdmission context",
        )
        raise AdmissionAuthorityError("SIZING_MISMATCH", str(intent.requested_qty))

    limit_px = _decimal_or_none(adm_inp.get("limit_price"))
    if limit_px is not None and (
        intent.limit_price is None or intent.limit_price != round_equity_price(limit_px)
    ):
        METRICS.counter(
            "broker_authority_rejected",
            help_text="Entry intent limit mismatch vs ApprovalAdmission context",
        )
        raise AdmissionAuthorityError("LIMIT_MISMATCH", str(intent.limit_price))

    stop_px = _decimal_or_none(adm_inp.get("stop_price"))
    if stop_px is not None and (
        intent.stop_price is None or intent.stop_price != round_equity_price(stop_px)
    ):
        METRICS.counter(
            "broker_authority_rejected",
            help_text="Entry intent stop mismatch vs ApprovalAdmission context",
        )
        raise AdmissionAuthorityError("STOP_MISMATCH", str(intent.stop_price))


def assert_authority_invariant(
    record: AdmissionRecord,
    opportunity: TradeOpportunity,
    intent: OrderIntent,
    *,
    now: datetime | None = None,
    broker_environment: str | None = None,
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

    assert_entry_broker_authority(
        intent,
        opportunity,
        record,
        broker_environment=broker_environment,
    )

    return record


def load_valid_approval_admission(
    record_id: UUID,
    *,
    opportunity: TradeOpportunity,
    intent: OrderIntent,
    now: datetime | None = None,
    broker_environment: str | None = None,
) -> AdmissionRecord:
    """Load and verify an ApprovalAdmission record for capital-path entry."""
    from trading.admission_records import ADMISSION_RECORDS

    record = ADMISSION_RECORDS.get(record_id)
    if record is None:
        raise AdmissionAuthorityError("ADMISSION_REQUIRED", "approval_record_missing")
    return assert_authority_invariant(
        record,
        opportunity,
        intent,
        now=now,
        broker_environment=broker_environment,
    )


def assert_entry_intent_has_admission(
    intent: OrderIntent,
    opportunity: TradeOpportunity,
    *,
    broker_environment: str | None = None,
) -> None:
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
        broker_environment=broker_environment,
    )
