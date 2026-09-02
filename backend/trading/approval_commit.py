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
) -> ApprovalBundle:
    """Persist claim + ApprovalAdmission + opp link + Entry intent before broker.

    SQL stores share one session/transaction. Memory stores share one process lock.
    """
    # Resolve at call time so test monkeypatches of ADMISSION_RECORDS apply.
    from trading import admission_records as adm_mod

    adm = admission_store or adm_mod.ADMISSION_RECORDS
    if isinstance(opportunity_store, OpportunityStore) and isinstance(
        intent_store, OrderIntentStore
    ):
        return _commit_sql(
            opportunity_id=opportunity_id,
            admission=admission,
            admission_input=admission_input,
            geometry_hash=geometry_hash,
            quote_ts=quote_ts,
            market_gate_ts=market_gate_ts,
            pipeline_run_id=pipeline_run_id,
            broker_name=broker_name,
            qty=qty,
            limit_px=limit_px,
            stop_px=stop_px,
            risk_snapshot=risk_snapshot,
            strategy_version=strategy_version,
            symbol=symbol,
            opportunity_store=opportunity_store,
            intent_store=intent_store,
            admission_store=adm,
            decision_version=decision_version,
        )
    return _commit_memory(
        opportunity_id=opportunity_id,
        admission=admission,
        admission_input=admission_input,
        geometry_hash=geometry_hash,
        quote_ts=quote_ts,
        market_gate_ts=market_gate_ts,
        pipeline_run_id=pipeline_run_id,
        broker_name=broker_name,
        qty=qty,
        limit_px=limit_px,
        stop_px=stop_px,
        risk_snapshot=risk_snapshot,
        strategy_version=strategy_version,
        symbol=symbol,
        opportunity_store=opportunity_store,
        intent_store=intent_store,
        admission_store=adm,
        decision_version=decision_version,
    )


def _fingerprint(admission_input: AdmissionInput, geometry_hash: str, decision_version: int) -> str:
    return build_request_fingerprint(
        admission_input,
        geometry_hash=geometry_hash,
        decision_version=decision_version,
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
) -> ApprovalBundle:
    fp = _fingerprint(admission_input, geometry_hash, decision_version)
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
        already_approving = row.status == OpportunityStatus.APPROVING.value
        if not already_approving:
            opp = opp.model_copy(
                update={
                    "status": OpportunityStatus.APPROVING,
                    "claimed_at": datetime.now(UTC),
                }
            )

        # Lost-reply / restart: reuse unresolved entry intent before writing
        # a new admission (fingerprint may differ only by wall-clock stamps).
        from database.models.desk import OrderIntentRow
        from trading.admission_records import StaleDecisionError
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
            if live.geometry_hash != geometry_hash:
                raise StaleDecisionError("unresolved_intent_fingerprint_mismatch")
            if live.approval_admission_record_id is None:
                raise StaleDecisionError("intent_missing_admission_fk")
            record = admission_store.get(live.approval_admission_record_id)
            if record is None:
                from database.models.desk import AdmissionRecordRow

                row_adm = session.get(AdmissionRecordRow, live.approval_admission_record_id)
                if row_adm is None:
                    raise StaleDecisionError("admission_missing_for_intent")
                record = AdmissionRecord.model_validate(row_adm.payload)
            # Qty is frozen on the unresolved intent — a lost-reply retry must
            # not re-size and treat the difference as STALE_DECISION.
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

        # An entry intent that already reached a terminal (or any) state means
        # this opportunity already took a shot at the broker. Minting a second
        # CREATED row under APPROVING is how concurrent approvers double-buy.
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
                "evaluated_at": admission_input.evaluated_at.isoformat(),
                "effective_rr": admission.effective_rr,
            },
            geometry_hash=geometry_hash,
            quote_ts=quote_ts,
            market_gate_ts=market_gate_ts,
            phase="approval",
            decision_version=decision_version,
            request_fingerprint=fp,
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
            symbol=symbol,
            qty=qty,
            limit_px=limit_px,
            stop_px=stop_px,
            risk_snapshot=risk_snapshot,
            strategy_version=strategy_version,
            approval_admission_record_id=record.id,
            geometry_hash=geometry_hash,
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
    symbol: str,
    qty: Decimal,
    limit_px: Decimal,
    stop_px: Decimal,
    risk_snapshot: dict[str, Any],
    strategy_version: str,
    approval_admission_record_id: UUID,
    geometry_hash: str,
) -> tuple[OrderIntent, bool]:
    from database.models.desk import OrderIntentRow
    from trading.intents import _from_row as intent_from_row

    prefix = f"entry:{opportunity_id}:"
    rows = (
        session.query(OrderIntentRow)
        .filter(OrderIntentRow.idempotency_key.startswith(prefix))
        .order_by(OrderIntentRow.created_at.asc())
        .all()
    )
    existing = [intent_from_row(r) for r in rows]
    candidate = OrderIntent(
        idempotency_key=entry_idempotency_key(opportunity_id, len(existing)),
        broker=broker_name,
        broker_account_id=None,
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
) -> ApprovalBundle:
    fp = _fingerprint(admission_input, geometry_hash, decision_version)
    with _MEMORY_BUNDLE_LOCK:
        opp = opportunity_store.get(opportunity_id)
        if opp is None:
            raise ValueError("opportunity_not_found")
        if opp.status not in {
            OpportunityStatus.AWAITING_CONFIRMATION,
            OpportunityStatus.APPROVING,
        }:
            raise ValueError(f"invalid_status:{opp.status.value}")

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
            from trading.admission_records import StaleDecisionError

            if live.geometry_hash != geometry_hash:
                raise StaleDecisionError("unresolved_intent_fingerprint_mismatch")
            if live.approval_admission_record_id is None:
                raise StaleDecisionError("intent_missing_admission_fk")
            record = admission_store.get(live.approval_admission_record_id)
            if record is None:
                raise StaleDecisionError("admission_missing_for_intent")
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
                "evaluated_at": admission_input.evaluated_at.isoformat(),
                "effective_rr": admission.effective_rr,
            },
            geometry_hash=geometry_hash,
            quote_ts=quote_ts,
            market_gate_ts=market_gate_ts,
            phase="approval",
            decision_version=decision_version,
            request_fingerprint=fp,
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

        candidate = OrderIntent(
            idempotency_key=entry_idempotency_key(opportunity_id, len(existing)),
            broker=broker_name,
            broker_account_id=None,
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
