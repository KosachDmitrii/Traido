"""Shared pipeline: candidate → risk → opportunity (no broker orders)."""

from __future__ import annotations

from uuid import uuid4

from agents.supervisor.agent import build_supervisor
from core.activity import BOARD
from core.audit import create_audit
from core.config import Settings, get_settings
from core.desk_bus import DESK_BUS
from core.enums import EntryDecision, MarketRegimeLabel, RiskVerdict, SessionCohort, Timeframe
from core.schemas import MarketAssessment, PipelineResult, RiskDecision
from notifications.telegram import get_notifier
from risk.context_builder import build_risk_context
from risk.limits import default_risk_limits
from risk.risk_engine import RiskEngine
from trading.entry_watches import ENTRY_WATCHES
from trading.opportunities import OPPORTUNITIES
from trading.scan_context import ScanContext, open_scan_context
from trading.shadow_policy import record_shadow_async

UNTRADABLE_REGIMES = {
    MarketRegimeLabel.BEARISH,
    MarketRegimeLabel.RISK_OFF,
    MarketRegimeLabel.HIGH_VOLATILITY,
}


def regime_allows_long(market: MarketAssessment | None) -> bool | None:
    """
    Translate the Market Agent's read into a long-only go/no-go.

    Returns None when there is no assessment, so the Risk Engine skips the
    check instead of assuming the tape is fine.
    """
    if market is None:
        return None
    return market.regime not in UNTRADABLE_REGIMES


async def run_symbol_pipeline(
    symbol: str,
    *,
    timeframes: tuple[Timeframe, ...] = (Timeframe.D1, Timeframe.H1),
    settings: Settings | None = None,
    publish: bool = True,
    context: ScanContext | None = None,
) -> PipelineResult:
    """Evaluate one symbol, and by default put the result on the desk.

    `publish=False` stops at the risk verdict and returns `risk_passed` without
    creating anything. A caller that intends to rank what it finds needs that:
    the desk has few slots, and a proposal that is written, notified and
    audited is one the operator can act on. Nothing may create one it means to
    take back.

    `context` carries the cycle's vendors. Passing one is how a scan avoids
    opening a broker connection per symbol — which against IBKR is not a cost
    but a refusal. Omitting it builds a single-symbol context, for the routes
    that evaluate one name on demand.
    """
    from trading.ledger import LEDGER

    # Asked before anything is measured. One open position per symbol is a rule
    # the desk enforces at the click, and it used to be enforced *only* there —
    # so a symbol already held was analysed in full, ranked, offered a slot and
    # notified about, and the resulting card could do nothing but collect a
    # `POSITION_ALREADY_OPEN`. The analysis is the expensive half of a cycle.
    if LEDGER.find_open_by_symbol(symbol) is not None:
        BOARD.set_agent("risk", status="idle", detail="Position already open", symbol=symbol)
        return PipelineResult(
            pipeline_run_id=uuid4(),
            symbol=symbol.upper(),
            status="position_open",
        )
    settings = settings or get_settings()
    if context is None:
        async with open_scan_context(settings) as solo:
            return await run_symbol_pipeline(
                symbol,
                timeframes=timeframes,
                settings=settings,
                publish=publish,
                context=solo,
            )
    # The cycle's own feed, not a new one. This factory used to be called here
    # once per symbol and built a fresh market-data adapter every time, which is
    # exactly what `ScanContext` was introduced to stop — and this was the one
    # path in the cycle still doing it.
    supervisor = build_supervisor(settings, market_data=context.market_data)
    result = await supervisor.scan_symbol(symbol, timeframes=timeframes)

    if result.candidate is None:
        BOARD.set_agent("risk", status="idle", detail="No candidate")
        return result

    # F3: shadow OLD (legacy would publish a BUY card) vs NEW entry decision.
    # Never places a second broker order.
    new_decision = result.candidate.entry_decision or EntryDecision.BUY_NOW
    if result.candidate.thesis is not None:
        await record_shadow_async(
            candidate=result.candidate,
            old_policy=EntryDecision.BUY_NOW,
            new_policy=new_decision,
            thesis=result.candidate.thesis,
            session_cohort=result.candidate.session_cohort or SessionCohort.UNKNOWN,
            entry_quality=result.candidate.entry_quality,
            chase_reasons=list(result.candidate.chase_reasons),
            reasons=list(result.candidate.reasons[:8]),
        )

    if new_decision is EntryDecision.NO_TRADE:
        BOARD.set_agent("risk", status="done", detail="NO_TRADE (entry timing)", symbol=symbol)
        BOARD.log("strategy", "NO_TRADE — thesis without edge at price", symbol=symbol)
        return result.model_copy(update={"status": "no_trade", "opportunity": None})

    if new_decision is EntryDecision.WAIT_FOR_ENTRY:
        bundle = result.entry_decision
        watch = None
        if bundle is not None:
            watch = ENTRY_WATCHES.create_from_bundle(result.candidate, bundle)
            audit = create_audit()
            await audit.append(
                "EntryWatchCreated",
                "entry_timing",
                watch.model_dump(mode="json"),
                pipeline_run_id=result.pipeline_run_id,
                entity_type="entry_watch",
                entity_id=str(watch.id),
            )
        BOARD.set_agent(
            "risk",
            status="done",
            detail=f"WAIT quality {result.candidate.entry_quality}",
            symbol=symbol,
        )
        BOARD.log(
            "strategy",
            f"WAIT_FOR_ENTRY · quality {result.candidate.entry_quality}/100 · "
            f"no BUY card",
            symbol=symbol,
        )
        return result.model_copy(
            update={
                "status": "wait_for_entry",
                "opportunity": None,
                "entry_watch": watch,
            }
        )

    existing = [o for o in OPPORTUNITIES.list_open() if o.candidate.symbol == symbol.upper()]
    if existing:
        BOARD.set_agent(
            "risk",
            status="done",
            detail="Existing opportunity",
            symbol=symbol,
        )
        return result.model_copy(
            update={
                "status": "awaiting_confirmation",
                "opportunity": existing[0],
                "risk": existing[0].risk,
            }
        )

    BOARD.set_agent("risk", status="working", detail="Checking limits", symbol=symbol)
    broker = context.broker
    audit = create_audit()
    portfolio = await context.portfolio()

    built = await build_risk_context(
        symbol,
        broker=broker,
        market_data=context.market_data,
        finnhub_api_key=settings.finnhub_api_key,
        regime_tradable=regime_allows_long(result.market),
        news=result.news.status if result.news else None,
    )
    for note in built.notes:
        BOARD.log("risk", note, symbol=symbol, level="warn")

    risk = RiskEngine(default_risk_limits()).evaluate(
        result.candidate, portfolio, context=built.context
    )

    await audit.append(
        "RiskDecisionRecorded",
        "risk_engine",
        {**risk.model_dump(mode="json"), "context_notes": built.notes},
        pipeline_run_id=result.pipeline_run_id,
    )

    if risk.verdict != RiskVerdict.PASS:
        BOARD.set_agent(
            "risk",
            status="done",
            detail="REJECT " + ",".join(risk.reasons[:2]),
            symbol=symbol,
            score=0,
        )
        BOARD.log(
            "risk",
            f"REJECTED: {', '.join(risk.reasons)}",
            symbol=symbol,
            level="warn",
        )
        return result.model_copy(
            update={"status": "risk_rejected", "risk": risk, "opportunity": None}
        )

    if not publish:
        BOARD.set_agent(
            "risk",
            status="done",
            detail=f"PASS qty {risk.sized_qty} · ranking",
            symbol=symbol,
            score=100,
        )
        return result.model_copy(
            update={"status": "risk_passed", "risk": risk, "opportunity": None}
        )

    return await publish_opportunity(result, risk, settings=settings)


async def publish_opportunity(
    result: PipelineResult,
    risk: RiskDecision,
    *,
    settings: Settings | None = None,
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

    # Re-checked at publish time: ranking happens after a full pass, and the
    # symbol may have gained a proposal while the rest of the universe scanned.
    existing = [o for o in OPPORTUNITIES.list_open() if o.candidate.symbol == symbol.upper()]
    if existing:
        return result.model_copy(
            update={
                "status": "awaiting_confirmation",
                "opportunity": existing[0],
                "risk": existing[0].risk,
            }
        )

    opp = OPPORTUNITIES.create(result.candidate, risk, settings.trading_mode)
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
    BOARD.set_agent("scanner", status="idle", detail="Proposal queued")
    return result.model_copy(
        update={
            "status": "awaiting_confirmation",
            "risk": risk,
            "opportunity": opp,
        }
    )
