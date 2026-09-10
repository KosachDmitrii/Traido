"""Atomic plan consumption, creation evidence, and a manually actionable proposal."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select

from core.desk_bus import DESK_BUS
from core.enums import AdmissionDecision, OpportunityStatus, RiskVerdict, TradingMode
from core.schemas import PipelineResult, TradeOpportunity
from database.models.orb import OrbSessionRow
from database.session import session_factory
from strategy.orb import OrbPlan
from trading.admission_records import ADMISSION_RECORDS
from trading.final_admission import FinalAdmissionEvaluation
from trading.opportunities import _write_payload


def publish_orb(
    result: PipelineResult,
    final: FinalAdmissionEvaluation,
    mode: TradingMode,
    *,
    now: datetime | None = None,
) -> TradeOpportunity:
    candidate, risk = result.candidate, result.risk
    if candidate is None or risk is None or risk.verdict != RiskVerdict.PASS:
        raise ValueError("ORB_RISK_REQUIRED")
    if (
        not final.admission.admitted
        or final.admission.decision is not AdmissionDecision.BUY_ALLOWED
    ):
        raise ValueError("ORB_ADMISSION_REQUIRED")
    plan = OrbPlan.model_validate(candidate.orb_plan)
    now = now or datetime.now(UTC)
    deadline = plan.entry_deadline
    if plan.version == "orb@2.0.0":
        deadline = min(deadline, datetime.fromisoformat(plan.evidence["retest"]["valid_until"]))
    if now >= deadline:
        raise ValueError("ORB_ENTRY_EXPIRED")
    with session_factory()() as db:
        row = db.scalar(
            select(OrbSessionRow).where(OrbSessionRow.session == plan.session).with_for_update()
        )
        if row is None or row.payload.get("plans", {}).get(plan.symbol) != plan.model_dump(
            mode="json"
        ):
            raise ValueError("ORB_PLAN_NOT_SELECTED")
        state = row.payload.get("states", {}).get(plan.symbol, {})
        if state.get("opportunity_id"):
            from database.models.desk import OpportunityRow
            from trading.opportunities import _from_row

            existing = db.get(OpportunityRow, UUID(state["opportunity_id"]))
            if existing is None:
                raise ValueError("ORB_PUBLICATION_UNRESOLVED")
            return _from_row(existing)
        if state.get("rearmed_at") and final.quote.ts < datetime.fromisoformat(state["rearmed_at"]):
            raise ValueError("ORB_STALE_REARM_ADMISSION")
        opp = TradeOpportunity(
            id=uuid4(),
            candidate=candidate,
            risk=risk,
            status=OpportunityStatus.AWAITING_CONFIRMATION,
            trading_mode=mode,
            created_at=now,
            expires_at=deadline,
            proposed_qty=risk.sized_qty,
            signal_detected_at=now,
            signal_price=plan.trigger,
            published_at=now,
            published_price=candidate.entry,
            geometry_hash=final.geometry_hash,
            policy_version=plan.version,
            legacy=False,
        )
        _write_payload(db, opp)
        db.flush()
        rec = ADMISSION_RECORDS.record_in_session(
            db,
            symbol=plan.symbol,
            admission=final.admission,
            opportunity_id=opp.id,
            pipeline_run_id=result.pipeline_run_id,
            geometry_hash=final.geometry_hash,
            quote_ts=final.quote.ts,
            phase="creation",
            context={
                "phase": "creation",
                "source": "orb",
                "admission_input": final.admission_input.model_dump(mode="json"),
            },
        )
        opp = opp.model_copy(
            update={
                "creation_admission_record_id": rec.id,
                "creation_admission_version": plan.version,
            }
        )
        _write_payload(db, opp)
        payload = deepcopy(row.payload)
        payload.setdefault("states", {})[plan.symbol] = {
            **state,
            "state": "BUY_ALLOWED",
            "reasons": [
                "ORB_PRICE_WITHIN_LIMIT"
                if plan.version == "orb@1.5.0"
                else "ORB_RETEST_CONFIRMED"
                if plan.version == "orb@2.0.0"
                else "ORB_BREAKOUT_CONFIRMED"
            ],
            "opportunity_id": str(opp.id),
            "observed_at": now.isoformat(),
        }
        row.payload = payload
        db.commit()
    DESK_BUS.bump_desk(kind="opportunity", symbol=plan.symbol, opportunity_id=str(opp.id))
    return opp
