"""Atomic pre-broker approval bundle: claim + admission + link + entry intent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from core.enums import IntentPurpose, IntentStatus, OpportunityStatus, OrderSide, OrderType
from core.schemas import AdmissionInput, AdmissionRecord, TradeAdmissionResult, TradeOpportunity
from database.models.desk import OpportunityRow
from database.session import session_factory
from trading.admission_authority import assert_authority_invariant
from trading.admission_records import (
    AdmissionRecordStore,
    build_request_fingerprint,
)
from trading.intents import OrderIntentStore, _write
from trading.opportunities import (
    OpportunityStore,
    _from_row,
    _write_payload,
)
from trading.order_intent import OrderIntent, entry_idempotency_key
from trading.pricing import round_equity_price, round_equity_qty

_MEMORY_BUNDLE_LOCK = Lock()


@dataclass(frozen=True)
class ApprovalBundle:
    opportunity: TradeOpportunity
    admission_record: AdmissionRecord
    intent: OrderIntent
    created_intent: bool


def commit_approval_bundle(
    *,
    opportunity_id: UUID,
    admission: TradeAdmissionResult,
    admission_input: AdmissionInput,
    geometry_hash: str,
    quote_ts: datetime | None,
    market_gate_ts: datetime | None,
    pipeline_run_id: UUID | None,
    broker_name: str,
    qty: Decimal,
    limit_px: Decimal,
    stop_px: Decimal,
    risk_snapshot: dict[str, Any],
    strategy_version: str,
    symbol: str,
    opportunity_store: Any,
    intent_store: Any,
    admission_store: AdmissionRecordStore | None = None,
    decision_version: int = 0,
    request_id: UUID | None = None,
    request_fingerprint: str | None = None,
    broker_account_id: str | None = None,
    broker_environment: str = "paper",
) -> ApprovalBundle:
    """Persist claim + ApprovalAdmission + opp link + Entry intent before broker.

    SQL stores share one session/transaction. Memory stores share one process lock.
    Reuse requires matching request_id + request_fingerprint (geometry alone is
    not enough).
    """
    # Resolve at call time so test monkeypatches of ADMISSION_RECORDS apply.
    from trading import admission_records as adm_mod

    adm = admission_store or adm_mod.ADMISSION_RECORDS
    fp = request_fingerprint or _fingerprint(
        admission_input,
        geometry_hash,
        decision_version,
        request_id=request_id,
        sized_qty=qty,
        limit_price=limit_px,
    )
    kwargs = {
        "opportunity_id": opportunity_id,
        "admission": admission,
        "admission_input": admission_input,
        "geometry_hash": geometry_hash,
        "quote_ts": quote_ts,
        "market_gate_ts": market_gate_ts,
        "pipeline_run_id": pipeline_run_id,
        "broker_name": broker_name,
        "qty": qty,
        "limit_px": limit_px,
        "stop_px": stop_px,
        "risk_snapshot": risk_snapshot,
        "strategy_version": strategy_version,
        "symbol": symbol,
        "opportunity_store": opportunity_store,
        "intent_store": intent_store,
        "decision_version": decision_version,
        "request_id": request_id,
        "request_fingerprint": fp,
        "broker_account_id": broker_account_id,
        "broker_environment": broker_environment,
        "admission_store": adm,
    }
    if isinstance(opportunity_store, OpportunityStore) and isinstance(
        intent_store, OrderIntentStore
    ):
        return _commit_sql(**kwargs)
    return _commit_memory(**kwargs)


def _fingerprint(
    admission_input: AdmissionInput,
    geometry_hash: str,
    decision_version: int,
    *,
    request_id: UUID | None = None,
    sized_qty: Decimal | None = None,
    limit_price: Decimal | None = None,
) -> str:
    return build_request_fingerprint(
        admission_input,
        geometry_hash=geometry_hash,
        decision_version=decision_version,
        request_id=request_id,
        sized_qty=sized_qty,
        limit_price=limit_price,
    )


def _commit_sql(
    *,
    opportunity_id: UUID,
    admission: TradeAdmissionResult,
    admission_input: AdmissionInput,
    geometry_hash: str,
    quote_ts: datetime | None,
    market_gate_ts: datetime | None,
    pipeline_run_id: UUID | None,
    broker_name: str,
    qty: Decimal,
    limit_px: Decimal,
    stop_px: Decimal,
    risk_snapshot: dict[str, Any],
    strategy_version: str,
    symbol: str,
    opportunity_store: OpportunityStore,
    intent_store: OrderIntentStore,
    admission_store: AdmissionRecordStore,
    decision_version: int,
    request_id: UUID | None,
    request_fingerprint: str,
    broker_account_id: str | None = None,
    broker_environment: str = "paper",
) -> ApprovalBundle:
    fp = request_fingerprint
    if broker_environment != "paper":
        from trading.approval_errors import DataBlockedError

        raise DataBlockedError("BROKER_ENVIRONMENT_BLOCKED")
    if not broker_account_id:
        from trading.approval_errors import DataBlockedError

        raise DataBlockedError("BROKER_ACCOUNT_IDENTITY_REQUIRED")
    engine = opportunity_store._engine
    if engine is None:
        from database.session import get_sync_engine

        engine = get_sync_engine()
    # Keep admission writes on the same engine as the opportunity claim —
    # a harness that pointed ADMISSION_RECORDS elsewhere would orphan the FK.
    if admission_store._engine is not engine:
        admission_store = AdmissionRecordStore(engine=engine)
        from trading import admission_records as adm_mod

        adm_mod.ADMISSION_RECORDS = admission_store
    SessionLocal = session_factory(engine)
    with (
        opportunity_store._lock,
        intent_store._lock,
        admission_store._lock,
        SessionLocal() as session,
    ):
        row = (
            session.query(OpportunityRow)
            .filter(
                OpportunityRow.id == opportunity_id,
                OpportunityRow.status.in_(
                    [
                        OpportunityStatus.AWAITING_CONFIRMATION.value,
                        OpportunityStatus.APPROVING.value,
                    ]
                ),
            )
            .with_for_update()
            .one_or_none()
        )
        if row is None:
            raise ValueError("invalid_status:missing_or_claimed")
        opp = _from_row(row)
        if opp.decision_version != decision_version:
            from trading.approval_errors import StaleDecisionError

            raise StaleDecisionError(f"decision_version:{opp.decision_version}!={decision_version}")
        already_approving = row.status == OpportunityStatus.APPROVING.value
        if not already_approving:
            opp = opp.model_copy(
                update={
                    "status": OpportunityStatus.APPROVING,
                    "claimed_at": datetime.now(UTC),
                }
            )

        from core.metrics import METRICS
        from database.models.desk import OrderIntentRow
        from trading.approval_errors import (
            EntryInFlightError,
            IdempotencyConflictError,
            StaleDecisionError,
        )
        from trading.intents import _from_row as intent_from_row

        prefix = f"entry:{opportunity_id}:"
        prior_rows = (
            session.query(OrderIntentRow)
            .filter(OrderIntentRow.idempotency_key.startswith(prefix))
            .order_by(OrderIntentRow.created_at.asc())
            .all()
        )
        prior = [intent_from_row(r) for r in prior_rows]
        live = next((i for i in prior if i.is_unresolved), None)
        if live is not None:
            if request_id is not None and live.request_id is not None:
                if live.request_id != request_id:
                    # Different user click while broker attempt still open.
                    METRICS.counter(
                        "entry_in_flight_blocked",
                        help_text="New APPROVE blocked by unresolved entry intent",
                    )
                    raise EntryInFlightError(str(live.id))
                prior_fp = live.request_fingerprint
                if prior_fp is None and live.approval_admission_record_id is not None:
                    adm_rec = admission_store.get(live.approval_admission_record_id)
                    prior_fp = adm_rec.request_fingerprint if adm_rec is not None else None
                # UNKNOWN + same request_id = transport retry after lost reply:
                # recover the existing intent; do not demand a fresh fingerprint
                # match (re-evaluation always drifts wall-clock snapshot fields).
                if live.status is IntentStatus.UNKNOWN:
                    prior_fp = fp
                if prior_fp is not None and prior_fp != fp:
                    METRICS.counter(
                        "approval_fingerprint_mismatch",
                        help_text="Same request_id with different ApprovalEvidence fingerprint",
                    )
                    raise IdempotencyConflictError(f"request_id={request_id}")
            elif live.geometry_hash != geometry_hash:
                raise StaleDecisionError("unresolved_intent_geometry_mismatch")
            if live.approval_admission_record_id is None:
                raise StaleDecisionError("intent_missing_admission_fk")
            record = admission_store.get(live.approval_admission_record_id)
            if record is None:
                from database.models.desk import AdmissionRecordRow

                row_adm = session.get(AdmissionRecordRow, live.approval_admission_record_id)
                if row_adm is None:
                    raise StaleDecisionError("admission_missing_for_intent")
                record = AdmissionRecord.model_validate(row_adm.payload)
            # Same request_id + fingerprint (or UNKNOWN recovery): reuse.
            if (
                record.request_fingerprint
                and record.request_fingerprint != fp
                and live.status is not IntentStatus.UNKNOWN
            ):
                if request_id is not None and live.request_id == request_id:
                    raise IdempotencyConflictError(f"request_id={request_id}")
                raise StaleDecisionError("admission_fingerprint_mismatch")
            METRICS.counter(
                "approval_idempotent_reuse",
                help_text="ApprovalBundle reused for transport retry",
            )
            opp = opp.model_copy(
                update={
                    "approval_admission_record_id": record.id,
                    "geometry_hash": geometry_hash,
                    "decision_version": decision_version,
                    "approved_at": datetime.now(UTC),
                    "approval_price": round_equity_price(limit_px),
                }
            )
            _write_payload(session, opp)
            assert_authority_invariant(record, opp, live)
            session.commit()
            return ApprovalBundle(
                opportunity=opp,
                admission_record=record,
                intent=live,
                created_intent=False,
            )

        if prior:
            raise ValueError("invalid_status:entry_intent_exists")
        if already_approving:
            raise ValueError("invalid_status:approving")

        record = admission_store.record_in_session(
            session,
            symbol=symbol,
            admission=admission,
            opportunity_id=opportunity_id,
            pipeline_run_id=pipeline_run_id,
            context={
                "source": "approval",
                "phase": "approval",
                "admission_input": admission_input.model_dump(mode="json"),
                "request_fingerprint": fp,
                "request_id": str(request_id) if request_id else None,
                "evaluated_at": admission_input.evaluated_at.isoformat(),
                "effective_rr": admission.effective_rr,
            },
            geometry_hash=geometry_hash,
            quote_ts=quote_ts,
            market_gate_ts=market_gate_ts,
            phase="approval",
            decision_version=decision_version,
            request_fingerprint=fp,
            request_id=request_id,
        )
        opp = opp.model_copy(
            update={
                "approval_admission_record_id": record.id,
                "geometry_hash": geometry_hash,
                "decision_version": decision_version,
                "approved_at": datetime.now(UTC),
                "approval_price": round_equity_price(limit_px),
            }
        )
        _write_payload(session, opp)

        intent, created = _intent_in_session(
            session,
            opportunity_id=opportunity_id,
            broker_name=broker_name,
            broker_account_id=broker_account_id,
            broker_environment=broker_environment,
            symbol=symbol,
            qty=qty,
            limit_px=limit_px,
            stop_px=stop_px,
            risk_snapshot=risk_snapshot,
            strategy_version=strategy_version,
            approval_admission_record_id=record.id,
            geometry_hash=geometry_hash,
            request_id=request_id,
            request_fingerprint=fp,
        )
        assert_authority_invariant(record, opp, intent)
        session.commit()
        return ApprovalBundle(
            opportunity=opp,
            admission_record=record,
            intent=intent,
            created_intent=created,
        )


def _intent_in_session(
    session: Session,
    *,
    opportunity_id: UUID,
    broker_name: str,
    broker_account_id: str,
    broker_environment: str,
    symbol: str,
    qty: Decimal,
    limit_px: Decimal,
    stop_px: Decimal,
    risk_snapshot: dict[str, Any],
    strategy_version: str,
    approval_admission_record_id: UUID,
    geometry_hash: str,
    request_id: UUID | None = None,
    request_fingerprint: str | None = None,
) -> tuple[OrderIntent, bool]:
    from database.models.desk import OrderIntentRow
    from trading.intents import _from_row as intent_from_row

    # Prefer request_id-scoped key when present (unique per click).
    if request_id is not None:
        key = f"entry:{opportunity_id}:{request_id.hex}"
    else:
        prefix = f"entry:{opportunity_id}:"
        rows = (
            session.query(OrderIntentRow)
            .filter(OrderIntentRow.idempotency_key.startswith(prefix))
            .order_by(OrderIntentRow.created_at.asc())
            .all()
        )
        existing = [intent_from_row(r) for r in rows]
        key = entry_idempotency_key(opportunity_id, len(existing))

    candidate = OrderIntent(
        idempotency_key=key,
        broker=broker_name,
        broker_account_id=broker_account_id,
        broker_environment=broker_environment,
        symbol=symbol,
        side=OrderSide.BUY,
        requested_qty=round_equity_qty(qty),
        order_type=OrderType.LIMIT,
        limit_price=round_equity_price(limit_px),
        stop_price=round_equity_price(stop_px),
        strategy_version=strategy_version,
        opportunity_id=opportunity_id,
        risk_snapshot=risk_snapshot,
        approval_admission_record_id=approval_admission_record_id,
        geometry_hash=geometry_hash,
        request_id=request_id,
        request_fingerprint=request_fingerprint,
        purpose=IntentPurpose.ENTRY,
        status=IntentStatus.CREATED,
    )
    _write(session, candidate)
    session.flush()
    return candidate, True


def _commit_memory(
    *,
    opportunity_id: UUID,
    admission: TradeAdmissionResult,
    admission_input: AdmissionInput,
    geometry_hash: str,
    quote_ts: datetime | None,
    market_gate_ts: datetime | None,
    pipeline_run_id: UUID | None,
    broker_name: str,
    qty: Decimal,
    limit_px: Decimal,
    stop_px: Decimal,
    risk_snapshot: dict[str, Any],
    strategy_version: str,
    symbol: str,
    opportunity_store: Any,
    intent_store: Any,
    admission_store: AdmissionRecordStore,
    decision_version: int,
    request_id: UUID | None,
    request_fingerprint: str,
    broker_account_id: str | None = None,
    broker_environment: str = "paper",
) -> ApprovalBundle:
    fp = request_fingerprint
    if broker_environment != "paper":
        from trading.approval_errors import DataBlockedError

        raise DataBlockedError("BROKER_ENVIRONMENT_BLOCKED")
    if not broker_account_id:
        from trading.approval_errors import DataBlockedError

        raise DataBlockedError("BROKER_ACCOUNT_IDENTITY_REQUIRED")
    with _MEMORY_BUNDLE_LOCK:
        opp = opportunity_store.get(opportunity_id)
        if opp is None:
            raise ValueError("opportunity_not_found")
        if opp.status not in {
            OpportunityStatus.AWAITING_CONFIRMATION,
            OpportunityStatus.APPROVING,
        }:
            raise ValueError(f"invalid_status:{opp.status.value}")
        if opp.decision_version != decision_version:
            from trading.approval_errors import StaleDecisionError

            raise StaleDecisionError(f"decision_version:{opp.decision_version}!={decision_version}")

        already_approving = opp.status is OpportunityStatus.APPROVING
        if not already_approving:
            claimed = opportunity_store.claim(
                opportunity_id,
                from_status=OpportunityStatus.AWAITING_CONFIRMATION,
                to_status=OpportunityStatus.APPROVING,
            )
            if claimed is None:
                raise ValueError("invalid_status:claim_lost")
            opp = claimed

        existing = intent_store.list_by_key_prefix(f"entry:{opportunity_id}:")
        live = next((i for i in existing if i.is_unresolved), None)
        if live is not None:
            from core.metrics import METRICS
            from trading.approval_errors import (
                EntryInFlightError,
                IdempotencyConflictError,
                StaleDecisionError,
            )

            if request_id is not None and live.request_id is not None:
                if live.request_id != request_id:
                    METRICS.counter(
                        "entry_in_flight_blocked",
                        help_text="New APPROVE blocked by unresolved entry intent",
                    )
                    raise EntryInFlightError(str(live.id))
                prior_fp = live.request_fingerprint
                if prior_fp is None and live.approval_admission_record_id is not None:
                    adm_rec = admission_store.get(live.approval_admission_record_id)
                    prior_fp = adm_rec.request_fingerprint if adm_rec is not None else None
                if live.status is IntentStatus.UNKNOWN:
                    prior_fp = fp
                if prior_fp is not None and prior_fp != fp:
                    METRICS.counter(
                        "approval_fingerprint_mismatch",
                        help_text="Same request_id with different ApprovalEvidence fingerprint",
                    )
                    raise IdempotencyConflictError(f"request_id={request_id}")
            elif live.geometry_hash != geometry_hash:
                raise StaleDecisionError("unresolved_intent_geometry_mismatch")
            if live.approval_admission_record_id is None:
                raise StaleDecisionError("intent_missing_admission_fk")
            record = admission_store.get(live.approval_admission_record_id)
            if record is None:
                raise StaleDecisionError("admission_missing_for_intent")
            if (
                record.request_fingerprint
                and record.request_fingerprint != fp
                and live.status is not IntentStatus.UNKNOWN
            ):
                if request_id is not None and live.request_id == request_id:
                    raise IdempotencyConflictError(f"request_id={request_id}")
                raise StaleDecisionError("admission_fingerprint_mismatch")
            METRICS.counter(
                "approval_idempotent_reuse",
                help_text="ApprovalBundle reused for transport retry",
            )
            opp = opp.model_copy(
                update={
                    "approval_admission_record_id": record.id,
                    "geometry_hash": geometry_hash,
                    "decision_version": decision_version,
                    "approved_at": datetime.now(UTC),
                    "approval_price": round_equity_price(limit_px),
                }
            )
            opportunity_store.update(opp)
            assert_authority_invariant(record, opp, live)
            return ApprovalBundle(
                opportunity=opp,
                admission_record=record,
                intent=live,
                created_intent=False,
            )

        if existing:
            raise ValueError("invalid_status:entry_intent_exists")
        if already_approving:
            raise ValueError("invalid_status:approving")

        record = admission_store.record(
            symbol=symbol,
            admission=admission,
            opportunity_id=opportunity_id,
            pipeline_run_id=pipeline_run_id,
            context={
                "source": "approval",
                "phase": "approval",
                "admission_input": admission_input.model_dump(mode="json"),
                "request_fingerprint": fp,
                "request_id": str(request_id) if request_id else None,
                "evaluated_at": admission_input.evaluated_at.isoformat(),
                "effective_rr": admission.effective_rr,
            },
            geometry_hash=geometry_hash,
            quote_ts=quote_ts,
            market_gate_ts=market_gate_ts,
            phase="approval",
            decision_version=decision_version,
            request_fingerprint=fp,
            request_id=request_id,
        )
        opp = opp.model_copy(
            update={
                "approval_admission_record_id": record.id,
                "geometry_hash": geometry_hash,
                "decision_version": decision_version,
                "approved_at": datetime.now(UTC),
                "approval_price": round_equity_price(limit_px),
            }
        )
        opportunity_store.update(opp)

        key = (
            f"entry:{opportunity_id}:{request_id.hex}"
            if request_id is not None
            else entry_idempotency_key(opportunity_id, len(existing))
        )
        candidate = OrderIntent(
            idempotency_key=key,
            broker=broker_name,
            broker_account_id=broker_account_id,
            broker_environment=broker_environment,
            symbol=symbol,
            side=OrderSide.BUY,
            requested_qty=round_equity_qty(qty),
            order_type=OrderType.LIMIT,
            limit_price=round_equity_price(limit_px),
            stop_price=round_equity_price(stop_px),
            strategy_version=strategy_version,
            opportunity_id=opportunity_id,
            risk_snapshot=risk_snapshot,
            approval_admission_record_id=record.id,
            geometry_hash=geometry_hash,
            request_id=request_id,
            request_fingerprint=fp,
            purpose=IntentPurpose.ENTRY,
            status=IntentStatus.CREATED,
        )
        intent, created = intent_store.create_or_get(candidate)

        assert_authority_invariant(record, opp, intent)
        return ApprovalBundle(
            opportunity=opp,
            admission_record=record,
            intent=intent,
            created_intent=created,
        )
