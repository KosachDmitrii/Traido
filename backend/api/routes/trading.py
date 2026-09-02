"""Opportunity confirmation + portfolio + kill switch."""

from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agents.scanner.agent import request_rescan, wake_scanner
from api.deps import build_execution_service
from broker.factory import create_broker
from core.audit import create_audit
from core.config import get_settings
from core.desk_bus import DESK_BUS
from core.enums import UserDecision
from core.schemas import PortfolioSnapshot, TradeAdmissionExplain, TradeOpportunity
from notifications.telegram import get_notifier
from risk.kill_switch import get_kill_switch_state, is_kill_switch_on, set_kill_switch
from trading.admission_authority import AdmissionAuthorityError
from trading.admission_records import AdmissionIdempotencyConflict
from trading.approval_errors import StaleDecisionError
from trading.opportunities import OPPORTUNITIES

router = APIRouter(prefix="/api/v1", tags=["trading"])


class DecisionBody(BaseModel):
    decision: UserDecision = Field(description="approve or skip")
    # Optional on approve: whole shares ≤ the live risk max. Omitted → risk size.
    qty: Decimal | None = Field(default=None, ge=0, description="Shares to buy (≤ risk max)")
    # Required on approve: one click → one request_id; transport retries reuse it.
    request_id: UUID | None = Field(default=None, description="Client idempotency id for APPROVE")
    expected_decision_version: int | None = Field(
        default=None, ge=0, description="Card decision_version the operator saw"
    )


class KillSwitchBody(BaseModel):
    enabled: bool
    reason: str | None = None


class EntryPolicyBody(BaseModel):
    aggressiveness: int = Field(
        ge=0, le=100, description="0=strict pullback, 100=buy nearer market"
    )


@router.get("/opportunities", response_model=list[TradeOpportunity])
async def list_opportunities() -> list[TradeOpportunity]:
    return OPPORTUNITIES.list_open()


@router.get("/opportunities/{opportunity_id}", response_model=TradeOpportunity)
async def get_opportunity(opportunity_id: UUID) -> TradeOpportunity:
    opp = OPPORTUNITIES.get(opportunity_id)
    if opp is None:
        raise HTTPException(status_code=404, detail="opportunity_not_found")
    return opp


@router.get("/admission/explain", response_model=TradeAdmissionExplain)
async def admission_explain(
    watch_id: UUID | None = None,
    opportunity_id: UUID | None = None,
    admission_record_id: UUID | None = None,
) -> TradeAdmissionExplain:
    from trading.explain_trade_admission import explain_trade_admission

    if not any([watch_id, opportunity_id, admission_record_id]):
        raise HTTPException(
            status_code=400,
            detail="provide watch_id, opportunity_id, or admission_record_id",
        )
    result = explain_trade_admission(
        watch_id=watch_id,
        opportunity_id=opportunity_id,
        admission_record_id=admission_record_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail="admission_explain_not_found")
    return result


@router.post("/opportunities/{opportunity_id}/decide", response_model=TradeOpportunity)
async def decide_opportunity(opportunity_id: UUID, body: DecisionBody) -> TradeOpportunity:
    if body.decision not in {UserDecision.APPROVE, UserDecision.SKIP}:
        raise HTTPException(status_code=400, detail="decision must be approve or skip")
    if body.decision == UserDecision.SKIP and body.qty is not None:
        raise HTTPException(status_code=400, detail="qty is only valid on approve")
    if body.decision == UserDecision.APPROVE and (
        body.request_id is None or body.expected_decision_version is None
    ):
        raise HTTPException(
            status_code=422,
            detail="STALE_DECISION:request_id_and_expected_decision_version_required",
        )
    service = build_execution_service()
    try:
        result = await service.decide(
            opportunity_id,
            body.decision,
            qty=body.qty,
            request_id=body.request_id,
            expected_decision_version=body.expected_decision_version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        from trading.approval_errors import ApprovalDomainError

        if isinstance(exc, ApprovalDomainError):
            DESK_BUS.bump_desk(kind="decide_failed", opportunity_id=str(opportunity_id))
            DESK_BUS.bump_broker(kind="decide_failed")
            raise HTTPException(status_code=exc.http_status, detail=str(exc)) from exc
        if isinstance(
            exc,
            (
                RuntimeError,
                AdmissionIdempotencyConflict,
                StaleDecisionError,
                AdmissionAuthorityError,
            ),
        ):
            DESK_BUS.bump_desk(kind="decide_failed", opportunity_id=str(opportunity_id))
            DESK_BUS.bump_broker(kind="decide_failed")
            detail = str(exc)
            status = 409
            if detail.startswith(("DATA_BLOCKED", "BUY_REJECTED")):
                status = 422
            raise HTTPException(status_code=status, detail=detail) from exc
        raise
    DESK_BUS.bump_desk(
        kind="decide",
        opportunity_id=str(opportunity_id),
        status=result.status.value,
    )
    if body.decision == UserDecision.APPROVE:
        DESK_BUS.bump_broker(kind="decide")
    # This decision may have been the one holding the queue full.
    wake_scanner()
    return result


@router.get("/portfolio", response_model=PortfolioSnapshot)
async def portfolio() -> PortfolioSnapshot:
    settings = get_settings()
    broker = create_broker(settings)
    snap = await broker.get_portfolio()
    return snap.model_copy(update={"kill_switch": is_kill_switch_on()})


@router.get("/kill-switch")
async def get_kill_switch() -> dict:
    state = get_kill_switch_state()
    return {
        "enabled": state.enabled,
        "source": state.source,
        "changed_at": state.changed_at,
        "actor": state.actor,
        "reason": state.reason,
    }


@router.post("/kill-switch")
async def post_kill_switch(body: KillSwitchBody) -> dict:
    enabled = set_kill_switch(body.enabled, actor="user", reason=body.reason or "")
    audit = create_audit()
    await audit.append(
        "KillSwitchUpdated",
        "user",
        {"enabled": enabled, "reason": body.reason or ""},
    )

    settings = get_settings()
    notifier = get_notifier(settings.telegram_bot_token, settings.telegram_chat_id)
    if notifier.configured:
        await notifier.send_kill_switch(enabled=enabled, actor="user")

    return {"enabled": enabled}


@router.get("/entry-policy")
async def get_entry_policy() -> dict:
    from trading.entry_policy import policy_payload

    return policy_payload()


@router.put("/entry-policy")
async def put_entry_policy(body: EntryPolicyBody) -> dict:
    """Operator knob: how far above SMA/VWAP a setup may still BUY.

    Saves first, then aborts any in-flight cycle and starts a fresh one so the
    pass always uses the level just chosen. Risk/liquidity/RTH/earnings/news
    gates are unchanged.
    """
    from trading.entry_policy import policy_payload, set_entry_aggressiveness

    set_entry_aggressiveness(body.aggressiveness, actor="user")
    audit = create_audit()
    await audit.append(
        "EntryPolicyUpdated",
        "user",
        {"aggressiveness": body.aggressiveness},
    )
    rescan = request_rescan(reason="entry_policy")
    DESK_BUS.bump_desk()
    payload = policy_payload()
    payload["rescan"] = rescan
    return payload
