"""Shared pipeline: candidate → risk → opportunity (no broker orders)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from core.activity import BOARD
from core.audit import create_audit
from core.config import Settings, get_settings
from core.desk_bus import DESK_BUS
from core.enums import (
    AdmissionDecision,
    EntryWatchStatus,
    InstrumentThesis,
    MarketRegimeLabel,
    Timeframe,
)
from core.ports import MarketDataPort
from core.schemas import (
    AdmissionSnapshot,
    Bar,
    EntryDecisionBundle,
    EntryWatch,
    MarketAssessment,
    PipelineResult,
    Quote,
    RiskDecision,
    TradeAdmissionResult,
    TradeCandidate,
)
from database.session import session_factory
from notifications.telegram import get_notifier
from trading.decision_outcome import DECISION_OUTCOMES
from trading.opportunities import OPPORTUNITIES, _write_payload, withdraw_unactionable
from trading.scan_context import ScanContext, open_scan_context
from trading.trade_admission import evaluate_trade_admission
from trading.zone_arrival import ZoneArrivalFacts, evaluate_zone_arrival, zone_arrival_required

UNTRADABLE_REGIMES = {
    MarketRegimeLabel.BEARISH,
    MarketRegimeLabel.RISK_OFF,
    MarketRegimeLabel.HIGH_VOLATILITY,
}


def regime_allows_long(
    market: MarketAssessment | None,
    *,
    now: datetime | None = None,
) -> bool | None:
    """
    Translate the Market Agent's read into a long-only go/no-go.

    Returns None when there is no assessment, so the Risk Engine skips the
    check instead of assuming the tape is fine. Stale assessments fail closed.
    """
    if market is None:
        return None
    from datetime import UTC
    from datetime import datetime as dt

    from core.enums import DataHealthStatus
    from trading.market_gate import evaluate_market_gate

    evaluated = now or dt.now(UTC)
    gate = evaluate_market_gate(market, now=evaluated, require_sector=False)
    if gate.status is DataHealthStatus.UNHEALTHY:
        return False
    return gate.tradable_long


def zone_arrival_for_admission(
    *,
    symbol: str,
    candidate: TradeCandidate,
    bundle: EntryDecisionBundle,
    bars: list[Bar],
) -> ZoneArrivalFacts | None:
    """Minimal watch shell so pipeline admission can score zone arrival."""
    if not zone_arrival_required(candidate.setup_type):
        return None
    if bundle.entry_zone_low is None or bundle.entry_zone_high is None:
        return None
    if len(bars) < 5:
        return None
    if candidate.target is None:
        return None
    now = datetime.now(UTC)
    atr = bundle.facts.atr if bundle.facts else None
    snap = AdmissionSnapshot(
        price_at_creation=float(bundle.facts.current_price),
        atr_at_creation=atr,
        setup_type=candidate.setup_type,
        entry_zone_low=float(bundle.entry_zone_low),
        entry_zone_high=float(bundle.entry_zone_high),
        setup_quality_at_creation=bundle.setup_quality or 0,
        entry_quality_at_creation=bundle.entry_quality or 0,
        stop_at_creation=float(candidate.stop),
        target_at_creation=float(candidate.target),
        effective_rr_at_creation=float(candidate.risk_reward or 2.0),
        created_at=now,
    )
    watch = EntryWatch(
        id=uuid4(),
        symbol=symbol.upper(),
        strategy_version=candidate.strategy_version or "pipeline",
        created_at=now,
        valid_until=now + timedelta(hours=4),
        thesis=candidate.thesis or InstrumentThesis.BULLISH,
        signal_price=candidate.entry,
        current_price_at_creation=Decimal(str(bundle.facts.current_price)),
        entry_zone_low=bundle.entry_zone_low,
        entry_zone_high=bundle.entry_zone_high,
        planned_entry=candidate.entry,
        planned_stop=candidate.stop,
        planned_target=candidate.target,
        entry_quality_at_creation=bundle.entry_quality or 50,
        setup_type=candidate.setup_type,
        setup_quality_at_creation=bundle.setup_quality or 0,
        admission_snapshot=snap,
        status=EntryWatchStatus.WAITING,
        pipeline_run_id=candidate.pipeline_run_id,
    )
    return evaluate_zone_arrival(
        watch,
        bars,
        atr=atr,
        current_price=float(bundle.facts.current_price),
    )


async def run_symbol_pipeline(
    symbol: str,
    *,
    timeframes: tuple[Timeframe, ...] = (Timeframe.D1, Timeframe.H1),
    settings: Settings | None = None,
    publish: bool = True,
    context: ScanContext | None = None,
) -> PipelineResult:
    """Evaluate only the persisted ORB session selection."""
    from strategy.orb.runtime import evaluate_symbol

    if context is None:
        async with open_scan_context(settings or get_settings()) as ctx:
            return await evaluate_symbol(symbol, ctx, publish=publish)
    return await evaluate_symbol(symbol, context, publish=publish)


async def publish_opportunity(
    result: PipelineResult,
    risk: RiskDecision,
    *,
    settings: Settings | None = None,
    admission: TradeAdmissionResult | None = None,
    quote: Quote | None = None,
    market_data: MarketDataPort | None = None,
) -> PipelineResult:
    """Put a risk-passed evaluation on the desk as something the human can act on.

    Separate from evaluation so a scan can see everything it found before
    spending one of the desk's few slots. Everything irreversible from the
    operator's point of view — the stored proposal, the Telegram message, the
    audit trail — happens here and only for what is actually offered.
    """
    settings = settings or get_settings()
    assert result.candidate is not None, "publishing requires a candidate"
    symbol = result.candidate.symbol
    audit = create_audit()

    if result.candidate.target_model is None or result.candidate.target_reachability is None:
        return result.model_copy(
            update={
                "status": "admission_required",
                "opportunity": None,
                "errors": ["TARGET_PLAN_REQUIRED"],
            }
        )

    # Re-checked at publish time: ranking happens after a full pass, and the
    # symbol may have gained a proposal while the rest of the universe scanned.
    # Clear terminal legacy cards first; otherwise their partial unique row can
    # prevent the repaired candidate from replacing them.
    withdraw_unactionable(OPPORTUNITIES)
    existing = [o for o in OPPORTUNITIES.list_open() if o.candidate.symbol == symbol.upper()]
    if existing:
        return result.model_copy(
            update={
                "status": "awaiting_confirmation",
                "opportunity": existing[0],
                "risk": existing[0].risk,
            }
        )

    from core.enums import DataHealthStatus

    adm = admission
    bundle = result.entry_decision
    if adm is None and bundle is not None:
        adm = evaluate_trade_admission(
            bundle=bundle,
            candidate=result.candidate,
            quote=quote,
            target_plan=bundle.target,
        )
    if adm is None:
        return result.model_copy(update={"status": "admission_required", "opportunity": None})
    if (
        adm.decision is not AdmissionDecision.BUY_ALLOWED
        or not adm.admitted
        or adm.data_status is DataHealthStatus.UNHEALTHY
    ):
        return result.model_copy(update={"status": "admission_blocked", "opportunity": None})

    # A BUY card is an actionable promise. Verify the same sector benchmark
    # before publishing it; approval still performs an independent fresh check.
    from trading.sector_assessment import get_sector_assessment_port

    sector = await get_sector_assessment_port().assess(
        symbol,
        market_data=market_data,
        now=datetime.now(UTC),
    )
    if sector.data_status is DataHealthStatus.UNHEALTHY or sector.tradable_long is None:
        reasons = tuple(sector.reason_codes or ("SECTOR_ASSESSMENT_MISSING",))
        BOARD.set_agent("risk", status="done", detail="DATA_BLOCKED (sector)", symbol=symbol)
        BOARD.log(
            "risk",
            f"DATA_BLOCKED · {','.join(reasons[:4])}",
            symbol=symbol,
            level="warn",
        )
        DECISION_OUTCOMES.record(
            symbol=symbol,
            stage="opportunity_creation",
            outcome="DATA_BLOCKED",
            primary_reason=reasons[0],
            reason_codes=reasons,
            admission=adm.decision,
            entry_decision=result.candidate.entry_decision,
            risk_verdict=risk.verdict,
            pipeline_run_id=result.pipeline_run_id,
        )
        return result.model_copy(
            update={
                "status": "data_blocked",
                "opportunity": None,
                "errors": list(reasons),
            }
        )
    if sector.tradable_long is False:
        reasons = tuple(sector.reason_codes or ("SECTOR_BLOCKED",))
        BOARD.set_agent("risk", status="done", detail="NO_TRADE (sector)", symbol=symbol)
        BOARD.log(
            "risk",
            f"NO_TRADE · {','.join(reasons[:4])}",
            symbol=symbol,
            level="warn",
        )
        DECISION_OUTCOMES.record(
            symbol=symbol,
            stage="opportunity_creation",
            outcome="NO_TRADE",
            primary_reason=reasons[0],
            reason_codes=reasons,
            admission=adm.decision,
            entry_decision=result.candidate.entry_decision,
            risk_verdict=risk.verdict,
            pipeline_run_id=result.pipeline_run_id,
        )
        return result.model_copy(
            update={
                "status": "no_trade",
                "opportunity": None,
                "errors": list(reasons),
            }
        )

    opp = OPPORTUNITIES.create(result.candidate, risk, settings.trading_mode)
    from trading.admission_records import persist_admission
    from trading.entry_policy import get_entry_thresholds
    from trading.geometry_hash import geometry_hash_from_candidate

    gh = geometry_hash_from_candidate(result.candidate)
    th = get_entry_thresholds()
    rec = persist_admission(
        symbol=symbol,
        admission=adm,
        opportunity_id=opp.id,
        pipeline_run_id=result.pipeline_run_id,
        context={
            "source": "publish_opportunity",
            "phase": "creation",
            "sector": sector.model_dump(mode="json"),
        },
        geometry_hash=gh,
        quote_ts=quote.ts if quote else None,
        phase="creation",
    )
    # Link opportunity row metadata (legacy=False for new cards).
    SessionLocal = session_factory()
    with SessionLocal() as session:
        _write_payload(
            session,
            opp,
            creation_admission_record_id=rec.id,
            geometry_hash=gh,
            policy_version=th.policy_version if hasattr(th, "policy_version") else "entry_policy@1",
            legacy=False,
        )
        session.commit()
    DESK_BUS.bump_desk(
        kind="opportunity",
        symbol=opp.candidate.symbol,
        opportunity_id=str(opp.id),
    )

    notifier = get_notifier(settings.telegram_bot_token, settings.telegram_chat_id)
    if notifier.configured:
        sent = await notifier.send_opportunity(opp)
        if not sent.sent:
            BOARD.log("risk", f"Telegram notify failed: {sent.detail}", level="warn")
    BOARD.set_agent(
        "risk",
        status="done",
        detail=f"PASS qty {risk.sized_qty}",
        symbol=symbol,
        score=100,
    )
    BOARD.log(
        "risk",
        f"PASS · qty {risk.sized_qty} · awaiting your BUY/SKIP",
        symbol=symbol,
    )
    await audit.append(
        "OpportunityCreated",
        "scanner",
        {"opportunity_id": str(opp.id), "symbol": symbol.upper()},
        pipeline_run_id=result.pipeline_run_id,
        entity_type="opportunity",
        entity_id=str(opp.id),
    )
    from trading.auto_trigger_policy import enqueue_auto_approve_opportunity

    enqueue_auto_approve_opportunity(
        opp.id,
        audit=audit,
        symbol=symbol.upper(),
    )
    BOARD.set_agent("scanner", status="idle", detail="Proposal queued")
    return result.model_copy(
        update={
            "status": "awaiting_confirmation",
            "risk": risk,
            "opportunity": opp,
        }
    )
