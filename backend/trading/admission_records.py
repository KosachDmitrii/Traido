"""Persist every TradeAdmission evaluation for audit and explain."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from core.schemas import AdmissionRecord, TradeAdmissionResult
from database.models.desk import AdmissionRecordRow
from database.session import session_factory

ADMISSION_ORCHESTRATION_VERSION = "final_admission@1"

ADMISSION_RECORD_TTL_SEC = 900.0


def build_evaluation_key(
    *,
    phase: str,
    entity_id: UUID | str,
    state_or_version: int | str,
    geometry_hash: str | None,
) -> str | None:
    if not geometry_hash:
        return None
    return f"{phase}:{entity_id}:{state_or_version}:{geometry_hash}"


class AdmissionIdempotencyConflict(Exception):
    """Same evaluation_key with a different canonical payload."""

    def __init__(self, evaluation_key: str) -> None:
        self.evaluation_key = evaluation_key
        super().__init__(f"admission_idempotency_conflict:{evaluation_key}")


# Identity / wall-clock stamps that change on every persist of the same decision.
_CANONICAL_DROP_KEYS = frozenset({"id", "recorded_at", "expires_at"})
_SNAPSHOT_DROP_KEYS = frozenset(
    {
        "created_at",
        "evaluated_at",
        "quote_ts",
        "last_bar_ts",
        "market_gate_ts",
        "structural_source_ts",
    }
)
_CONTEXT_DROP_KEYS = frozenset({"evaluated_at"})


def _scrub_for_canonical(value: Any) -> Any:
    """Drop stamps that are not part of the admission decision itself.

    Retries after a lost broker reply re-evaluate and re-persist under the same
    evaluation_key. `build_admission_snapshot` always stamps `created_at=now`,
    so comparing the raw payload would treat every honest retry as a conflict.
    """
    if isinstance(value, dict):
        return {k: _scrub_for_canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_for_canonical(v) for v in value]
    return value


def _canonical_payload(data: dict[str, Any]) -> str:
    import json

    scrubbed = {k: v for k, v in data.items() if k not in _CANONICAL_DROP_KEYS}
    snap = scrubbed.get("admission_snapshot")
    if isinstance(snap, dict):
        scrubbed["admission_snapshot"] = {
            k: v for k, v in snap.items() if k not in _SNAPSHOT_DROP_KEYS
        }
    ctx = scrubbed.get("context")
    if isinstance(ctx, dict):
        scrubbed["context"] = {k: v for k, v in ctx.items() if k not in _CONTEXT_DROP_KEYS}
    return json.dumps(_scrub_for_canonical(scrubbed), sort_keys=True, default=str)


class AdmissionRecordStore:
    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine
        self._lock = Lock()

    def _sf(self) -> sessionmaker[Session]:
        return session_factory(self._engine)

    def record(
        self,
        *,
        symbol: str,
        admission: TradeAdmissionResult,
        watch_id: UUID | None = None,
        opportunity_id: UUID | None = None,
        pipeline_run_id: UUID | None = None,
        trigger_version: int | None = None,
        zone_arrival_quality: int | None = None,
        zone_arrival_type: str | None = None,
        context: dict[str, Any] | None = None,
        geometry_hash: str | None = None,
        quote_ts: datetime | None = None,
        market_gate_ts: datetime | None = None,
        phase: str | None = None,
    ) -> AdmissionRecord:
        now = datetime.now(UTC)
        ctx = dict(context or {})
        phase_val = phase or ctx.get("phase") or "creation"
        entity_id = watch_id or opportunity_id or pipeline_run_id or symbol
        version = trigger_version if trigger_version is not None else 0
        eval_key = build_evaluation_key(
            phase=phase_val,
            entity_id=entity_id,
            state_or_version=version,
            geometry_hash=geometry_hash,
        )
        rec_id = uuid4()
        expires = now + timedelta(seconds=ADMISSION_RECORD_TTL_SEC)
        record = AdmissionRecord(
            id=rec_id,
            symbol=symbol.upper(),
            recorded_at=now,
            decision=admission.decision,
            admitted=admission.admitted,
            setup_type=admission.setup_type,
            setup_quality=admission.setup_quality,
            entry_quality=admission.entry_quality,
            zone_arrival_quality=zone_arrival_quality,
            zone_arrival_type=zone_arrival_type,
            effective_rr=admission.effective_rr,
            chase_score=admission.chase_score,
            structure_valid=admission.structure_valid,
            stop_valid=admission.stop_valid,
            target_valid=admission.target_valid,
            data_status=admission.data_status,
            vetoes=list(admission.vetoes),
            warnings=list(admission.warnings),
            reason_codes=list(admission.reason_codes),
            admission_version=admission.admission_version,
            watch_id=watch_id,
            opportunity_id=opportunity_id,
            pipeline_run_id=pipeline_run_id,
            trigger_version=trigger_version,
            context=ctx,
            evaluation_key=eval_key,
            phase=phase_val,
            geometry_hash=geometry_hash,
            quote_ts=quote_ts,
            market_gate_ts=market_gate_ts,
            expires_at=expires,
            source_version=ADMISSION_ORCHESTRATION_VERSION,
            admission_input=ctx.get("admission_input"),
            admission_snapshot=admission.snapshot,
        )
        data = record.model_dump(mode="json")
        canonical = _canonical_payload(data)
        with self._lock:
            SessionLocal = self._sf()
            with SessionLocal() as session:
                if eval_key:
                    existing = (
                        session.query(AdmissionRecordRow)
                        .filter(AdmissionRecordRow.evaluation_key == eval_key)
                        .first()
                    )
                    if existing is not None:
                        existing_rec = AdmissionRecord.model_validate(existing.payload)
                        if existing_rec.expires_at is not None:
                            exp = existing_rec.expires_at
                            if exp.tzinfo is None:
                                exp = exp.replace(tzinfo=UTC)
                            if exp <= now:
                                raise AdmissionIdempotencyConflict(eval_key)
                        if _canonical_payload(existing.payload) != canonical:
                            raise AdmissionIdempotencyConflict(eval_key)
                        return existing_rec
                try:
                    session.add(
                        AdmissionRecordRow(
                            id=rec_id,
                            symbol=record.symbol,
                            recorded_at=now,
                            decision=admission.decision.value,
                            watch_id=watch_id,
                            opportunity_id=opportunity_id,
                            pipeline_run_id=pipeline_run_id,
                            payload=data,
                            evaluation_key=eval_key,
                            phase=phase_val,
                            geometry_hash=geometry_hash,
                            quote_ts=quote_ts,
                            market_gate_ts=market_gate_ts,
                            expires_at=expires,
                            source_version=ADMISSION_ORCHESTRATION_VERSION,
                        )
                    )
                    session.commit()
                except IntegrityError:
                    session.rollback()
                    if eval_key:
                        existing = (
                            session.query(AdmissionRecordRow)
                            .filter(AdmissionRecordRow.evaluation_key == eval_key)
                            .first()
                        )
                        if existing is not None:
                            if _canonical_payload(existing.payload) != canonical:
                                raise AdmissionIdempotencyConflict(eval_key)
                            return AdmissionRecord.model_validate(existing.payload)
                    raise
        return record

    def get(self, record_id: UUID) -> AdmissionRecord | None:
        SessionLocal = self._sf()
        with SessionLocal() as session:
            row = session.get(AdmissionRecordRow, record_id)
            if row is None:
                return None
            return AdmissionRecord.model_validate(row.payload)

    def latest_for_watch(self, watch_id: UUID) -> AdmissionRecord | None:
        SessionLocal = self._sf()
        with SessionLocal() as session:
            row = (
                session.query(AdmissionRecordRow)
                .filter(AdmissionRecordRow.watch_id == watch_id)
                .order_by(AdmissionRecordRow.recorded_at.desc())
                .first()
            )
            if row is None:
                return None
            return AdmissionRecord.model_validate(row.payload)

    def latest_for_opportunity(self, opportunity_id: UUID) -> AdmissionRecord | None:
        SessionLocal = self._sf()
        with SessionLocal() as session:
            row = (
                session.query(AdmissionRecordRow)
                .filter(AdmissionRecordRow.opportunity_id == opportunity_id)
                .order_by(AdmissionRecordRow.recorded_at.desc())
                .first()
            )
            if row is None:
                return None
            return AdmissionRecord.model_validate(row.payload)


ADMISSION_RECORDS = AdmissionRecordStore()


async def audit_admission_async(
    record: AdmissionRecord,
    *,
    actor: str = "trade_admission",
) -> None:
    from core.audit import create_audit

    audit = create_audit()
    await audit.append(
        "TradeAdmissionEvaluated",
        actor,
        record.model_dump(mode="json"),
        pipeline_run_id=record.pipeline_run_id,
        entity_type="watch" if record.watch_id else "symbol",
        entity_id=str(record.watch_id or record.symbol),
    )


def persist_admission(
    *,
    symbol: str,
    admission: TradeAdmissionResult,
    watch_id: UUID | None = None,
    opportunity_id: UUID | None = None,
    pipeline_run_id: UUID | None = None,
    trigger_version: int | None = None,
    zone_arrival_quality: int | None = None,
    zone_arrival_type: str | None = None,
    context: dict[str, Any] | None = None,
    geometry_hash: str | None = None,
    quote_ts: datetime | None = None,
    market_gate_ts: datetime | None = None,
    phase: str | None = None,
    audit: bool = True,
) -> AdmissionRecord:
    """Sync persist — critical path; audit is best-effort async only."""
    ctx = dict(context or {})
    phase_val = phase or ctx.get("phase")
    record = ADMISSION_RECORDS.record(
        symbol=symbol,
        admission=admission,
        watch_id=watch_id,
        opportunity_id=opportunity_id,
        pipeline_run_id=pipeline_run_id,
        trigger_version=trigger_version,
        zone_arrival_quality=zone_arrival_quality,
        zone_arrival_type=zone_arrival_type,
        context=ctx,
        geometry_hash=geometry_hash,
        quote_ts=quote_ts,
        market_gate_ts=market_gate_ts,
        phase=phase_val,
    )
    if audit:
        try:
            import asyncio

            loop = asyncio.get_running_loop()
            loop.create_task(audit_admission_async(record))
        except RuntimeError:
            pass
    return record
