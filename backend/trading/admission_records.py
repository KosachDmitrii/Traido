"""Persist every TradeAdmission evaluation for audit and explain."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from core.schemas import AdmissionInput, AdmissionRecord, TradeAdmissionResult
from database.models.desk import AdmissionRecordRow
from database.session import session_factory
from trading.approval_errors import StaleDecisionError

ADMISSION_ORCHESTRATION_VERSION = "final_admission@1"

ADMISSION_RECORD_TTL_SEC = 900.0

# Only technical persistence fields — never decision-determining source timestamps.
_FINGERPRINT_DROP = frozenset(
    {
        "id",
        "recorded_at",
        "expires_at",
        "created_at",
        "updated_at",
        "evaluated_at",  # wall-clock of this evaluation pass; source ts stay
    }
)


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


def build_request_fingerprint(
    admission_input: AdmissionInput | dict[str, Any],
    *,
    geometry_hash: str,
    decision_version: int,
    request_id: UUID | str | None = None,
    sized_qty: Any = None,
    limit_price: Any = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Stable hash of ApprovalEvidence facts — includes source timestamps.

    Spec: do not globally strip quote/bar/market/sector source timestamps.
    Only drop technical persistence fields that do not determine the decision.
    """
    if isinstance(admission_input, AdmissionInput):
        raw = admission_input.model_dump(mode="json")
    else:
        raw = dict(admission_input)
    scrubbed = _drop_fingerprint_stamps(raw)
    scrubbed["geometry_hash"] = geometry_hash
    scrubbed["decision_version"] = int(decision_version)
    if request_id is not None:
        scrubbed["request_id"] = str(request_id)
    if sized_qty is not None:
        scrubbed["sized_qty"] = str(sized_qty)
    if limit_price is not None:
        scrubbed["limit_price"] = str(limit_price)
    if extra:
        scrubbed.update(extra)
    payload = json.dumps(scrubbed, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def _drop_fingerprint_stamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: _drop_fingerprint_stamps(v) for k, v in value.items() if k not in _FINGERPRINT_DROP
        }
    if isinstance(value, list):
        return [_drop_fingerprint_stamps(v) for v in value]
    return value


class AdmissionIdempotencyConflict(Exception):
    """Same evaluation_key with a different canonical payload (legacy)."""

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
    if isinstance(value, dict):
        return {k: _scrub_for_canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_for_canonical(v) for v in value]
    return value


def _canonical_payload(data: dict[str, Any]) -> str:
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


def _fingerprint_of(record: AdmissionRecord | dict[str, Any]) -> str | None:
    if isinstance(record, AdmissionRecord):
        ctx = record.context or {}
        fp = ctx.get("request_fingerprint")
        if fp:
            return str(fp)
        return None
    ctx = record.get("context") or {}
    fp = ctx.get("request_fingerprint")
    return str(fp) if fp else None


class AdmissionRecordStore:
    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine
        self._lock = Lock()

    def _sf(self) -> sessionmaker[Session]:
        return session_factory(self._engine)

    def _build_record(
        self,
        *,
        symbol: str,
        admission: TradeAdmissionResult,
        watch_id: UUID | None,
        opportunity_id: UUID | None,
        pipeline_run_id: UUID | None,
        trigger_version: int | None,
        zone_arrival_quality: int | None,
        zone_arrival_type: str | None,
        context: dict[str, Any],
        geometry_hash: str | None,
        quote_ts: datetime | None,
        market_gate_ts: datetime | None,
        phase: str | None,
        decision_version: int | None,
        request_fingerprint: str | None,
        now: datetime,
    ) -> tuple[AdmissionRecord, str | None]:
        ctx = dict(context)
        phase_val = phase or ctx.get("phase") or "creation"
        entity_id = watch_id or opportunity_id or pipeline_run_id or symbol
        if decision_version is not None:
            version: int | str = decision_version
        elif trigger_version is not None:
            version = trigger_version
        else:
            version = 0
        eval_key = build_evaluation_key(
            phase=phase_val,
            entity_id=entity_id,
            state_or_version=version,
            geometry_hash=geometry_hash,
        )
        if request_fingerprint:
            ctx["request_fingerprint"] = request_fingerprint
        if "evaluated_at" not in ctx:
            ctx["evaluated_at"] = now.isoformat()
        rec_id = uuid4()
        expires = now + timedelta(seconds=ADMISSION_RECORD_TTL_SEC)
        admission_input = ctx.get("admission_input")
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
            admission_input=admission_input if isinstance(admission_input, dict) else None,
            admission_snapshot=admission.snapshot,
            request_fingerprint=request_fingerprint,
        )
        return record, eval_key

    def _resolve_existing(
        self,
        existing_rec: AdmissionRecord,
        *,
        eval_key: str,
        request_fingerprint: str | None,
        canonical: str,
        now: datetime,
    ) -> AdmissionRecord:
        if existing_rec.expires_at is not None:
            exp = existing_rec.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=UTC)
            if exp <= now:
                raise StaleDecisionError("admission_expired")
        existing_fp = _fingerprint_of(existing_rec) or existing_rec.request_fingerprint
        if request_fingerprint and existing_fp:
            if existing_fp == request_fingerprint:
                return existing_rec
            raise StaleDecisionError(f"fingerprint_mismatch:{eval_key}")
        if _canonical_payload(existing_rec.model_dump(mode="json")) != canonical:
            if request_fingerprint or existing_fp:
                raise StaleDecisionError(f"fingerprint_mismatch:{eval_key}")
            raise AdmissionIdempotencyConflict(eval_key)
        return existing_rec

    def record_in_session(
        self,
        session: Session,
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
        decision_version: int | None = None,
        request_fingerprint: str | None = None,
        request_id: UUID | str | None = None,
        now: datetime | None = None,
    ) -> AdmissionRecord:
        """Persist inside a caller-owned transaction (no commit)."""
        now = now or datetime.now(UTC)
        ctx = dict(context or {})
        if request_id is not None:
            ctx.setdefault("request_id", str(request_id))
        record, eval_key = self._build_record(
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
            phase=phase,
            decision_version=decision_version,
            request_fingerprint=request_fingerprint,
            now=now,
        )
        data = record.model_dump(mode="json")
        canonical = _canonical_payload(data)
        phase_val = record.phase or "creation"
        if eval_key:
            existing = (
                session.query(AdmissionRecordRow)
                .filter(AdmissionRecordRow.evaluation_key == eval_key)
                .first()
            )
            if existing is not None:
                existing_rec = AdmissionRecord.model_validate(existing.payload)
                return self._resolve_existing(
                    existing_rec,
                    eval_key=eval_key,
                    request_fingerprint=request_fingerprint,
                    canonical=canonical,
                    now=now,
                )
        req_col = None
        if request_id is not None:
            req_col = (
                request_id.hex if isinstance(request_id, UUID) else str(request_id).replace("-", "")
            )
            if len(req_col) > 32:
                req_col = str(request_id)
        session.add(
            AdmissionRecordRow(
                id=record.id,
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
                expires_at=record.expires_at,
                source_version=ADMISSION_ORCHESTRATION_VERSION,
                request_fingerprint=request_fingerprint,
                request_id=req_col,
            )
        )
        session.flush()
        return record

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
        decision_version: int | None = None,
        request_fingerprint: str | None = None,
        request_id: UUID | str | None = None,
    ) -> AdmissionRecord:
        now = datetime.now(UTC)
        with self._lock:
            SessionLocal = self._sf()
            with SessionLocal() as session:
                try:
                    record = self.record_in_session(
                        session,
                        symbol=symbol,
                        admission=admission,
                        watch_id=watch_id,
                        opportunity_id=opportunity_id,
                        pipeline_run_id=pipeline_run_id,
                        trigger_version=trigger_version,
                        zone_arrival_quality=zone_arrival_quality,
                        zone_arrival_type=zone_arrival_type,
                        context=context,
                        geometry_hash=geometry_hash,
                        quote_ts=quote_ts,
                        market_gate_ts=market_gate_ts,
                        phase=phase,
                        decision_version=decision_version,
                        request_fingerprint=request_fingerprint,
                        request_id=request_id,
                        now=now,
                    )
                    session.commit()
                    return record
                except IntegrityError:
                    session.rollback()
                    ctx = dict(context or {})
                    phase_val = phase or ctx.get("phase") or "creation"
                    entity_id = watch_id or opportunity_id or pipeline_run_id or symbol
                    version = (
                        decision_version
                        if decision_version is not None
                        else (trigger_version if trigger_version is not None else 0)
                    )
                    eval_key = build_evaluation_key(
                        phase=phase_val,
                        entity_id=entity_id,
                        state_or_version=version,
                        geometry_hash=geometry_hash,
                    )
                    if eval_key:
                        existing = (
                            session.query(AdmissionRecordRow)
                            .filter(AdmissionRecordRow.evaluation_key == eval_key)
                            .first()
                        )
                        if existing is not None:
                            existing_rec = AdmissionRecord.model_validate(existing.payload)
                            built, _ = self._build_record(
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
                                phase=phase,
                                decision_version=decision_version,
                                request_fingerprint=request_fingerprint,
                                now=now,
                            )
                            return self._resolve_existing(
                                existing_rec,
                                eval_key=eval_key,
                                request_fingerprint=request_fingerprint,
                                canonical=_canonical_payload(built.model_dump(mode="json")),
                                now=now,
                            )
                    raise

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
    decision_version: int | None = None,
    request_fingerprint: str | None = None,
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
        decision_version=decision_version,
        request_fingerprint=request_fingerprint,
    )
    if audit:
        try:
            import asyncio

            loop = asyncio.get_running_loop()
            loop.create_task(audit_admission_async(record))
        except RuntimeError:
            pass
    return record
