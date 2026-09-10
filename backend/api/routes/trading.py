"""Opportunity confirmation + portfolio + kill switch."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from agents.scanner.agent import wake_scanner
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


class PaperRiskStartBody(BaseModel):
    account_id: str = Field(min_length=1)
    confirmation: Literal["START_NEW_OBSERVED_PAPER_PERIOD"]


class PaperRiskSuspendBody(BaseModel):
    account_id: str = Field(min_length=1)
    confirmation: Literal["SUSPEND_PAPER_PERIOD"]


async def _alpaca_risk_snapshot():
    from broker.alpaca import AlpacaPaperBroker
    from broker.interface import broker_connection_state
    from core.enums import BrokerConnectionState

    broker = create_broker(get_settings())
    if not isinstance(broker, AlpacaPaperBroker) or broker.environment != "paper":
        raise HTTPException(status_code=409, detail="ALPACA_PAPER_REQUIRED")
    snapshot = await broker.get_portfolio(fresh=True)
    if broker_connection_state(broker) is not BrokerConnectionState.READY:
        raise HTTPException(status_code=409, detail="ALPACA_NOT_READY")
    if not snapshot.risk_account_id or snapshot.base_currency != "USD":
        raise HTTPException(status_code=409, detail="ALPACA_PAPER_ACCOUNT_UNVERIFIED")
    return broker, snapshot


@router.get("/risk-period", response_model=PortfolioSnapshot)
async def get_risk_period() -> PortfolioSnapshot:
    _, snapshot = await _alpaca_risk_snapshot()
    return snapshot


@router.post("/risk-period/start", response_model=PortfolioSnapshot)
async def start_risk_period(body: PaperRiskStartBody) -> PortfolioSnapshot:
    from risk.paper_period import RiskPeriodError, start_period

    _, snapshot = await _alpaca_risk_snapshot()
    if snapshot.risk_account_id != body.account_id:
        raise HTTPException(status_code=409, detail="RISK_ACCOUNT_CHANGED")
    if snapshot.risk_period_id:
        if snapshot.risk_history_status == "suspended":
            raise HTTPException(
                status_code=409, detail="RISK_PERIOD_SUSPENDED_RECONCILIATION_REQUIRED"
            )
        return snapshot  # A retry is not a request to erase intervening losses.
    if snapshot.risk_history_status != "not_started":
        raise HTTPException(status_code=409, detail="RISK_HISTORY_UNAVAILABLE")
    # Starting observation does not authorize execution or reconcile positions.
    # Existing exposure is already included in NetLiquidation. Entry gates remain
    # independent; an orphan/UNKNOWN is not resolved by recording this baseline.
    _, fresh = await _alpaca_risk_snapshot()
    if fresh.risk_account_id != body.account_id:
        raise HTTPException(status_code=409, detail="RISK_ACCOUNT_CHANGED")
    try:
        period = start_period(
            body.account_id, fresh.base_currency or "", fresh.equity, datetime.now(UTC)
        )
    except RiskPeriodError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    DESK_BUS.bump_desk()
    wake_scanner()
    return fresh.model_copy(update=period.metrics())


@router.post("/risk-period/suspend", response_model=PortfolioSnapshot)
async def suspend_risk_period(body: PaperRiskSuspendBody) -> PortfolioSnapshot:
    from risk.paper_period import RiskPeriodError, suspend_period

    _, snapshot = await _alpaca_risk_snapshot()
    if snapshot.risk_account_id != body.account_id:
        raise HTTPException(status_code=409, detail="RISK_ACCOUNT_CHANGED")
    try:
        period = suspend_period(body.account_id, snapshot.base_currency or "")
    except RiskPeriodError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    DESK_BUS.bump_desk()
    return snapshot.model_copy(update=period.metrics())


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
    aggressiveness: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Legacy alias for buy_confirmation_strictness",
    )
    buy_confirmation_strictness: int | None = Field(
        default=None,
        ge=0,
        le=100,
        description="0=strong BUY confirms, 100=weak BUY confirms",
    )


class AutoTriggerBody(BaseModel):
    enabled: bool = Field(description="Auto-approve BUY cards after TRIGGERED admission")


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
    return {"strategy": "orb@1.1.0", "retired": True}


@router.put("/entry-policy")
async def put_entry_policy(body: EntryPolicyBody) -> dict:
    raise HTTPException(status_code=410, detail="STRATEGY_RETIRED_ORB_FIXED_RULES")


@router.get("/auto-trigger")
async def get_auto_trigger() -> dict:
    from trading.auto_trigger_policy import policy_payload

    return policy_payload()


@router.put("/auto-trigger")
async def put_auto_trigger(body: AutoTriggerBody) -> dict:
    from trading.auto_trigger_policy import policy_payload, set_auto_trigger_enabled

    if body.enabled and not policy_payload()["available"]:
        raise HTTPException(status_code=409, detail="AUTO_TRIGGER_PAPER_ONLY")
    set_auto_trigger_enabled(body.enabled, actor="user")
    DESK_BUS.bump_desk(kind="auto_trigger_policy")
    return policy_payload()


async def _broker_backend_status() -> dict:
    from broker.backend_policy import broker_backend_payload
    from broker.factory import create_broker
    from broker.interface import broker_connection_state
    from core.config import get_settings

    payload = broker_backend_payload()
    try:
        broker = create_broker(get_settings())
        await broker.get_portfolio()
        payload["connection_state"] = broker_connection_state(broker).value
        account = getattr(broker, "account_id", None)
        if account:
            payload["account_id"] = account
        payload["broker_class"] = type(broker).__name__
    except Exception as exc:  # noqa: BLE001
        payload["connection_state"] = "disconnected"
        payload["error"] = type(exc).__name__
    return payload


@router.get("/broker-backend")
async def get_broker_backend_route() -> dict:
    return await _broker_backend_status()


@router.put("/broker-backend")
async def put_broker_backend() -> dict:
    raise HTTPException(status_code=410, detail="ALPACA_ONLY_BROKER_SELECTION_REMOVED")
