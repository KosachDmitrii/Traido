"""Trader desk orchestrator — one agent per professional step, Alpaca data only.

Order: context → universe → structure → setup → entry → risk_plan → checklist.
BUY_NOW requires checklist. WAIT_FOR_ENTRY builds a candidate so the pipeline
can publish an EntryWatch card — never places an order.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from agents.trader.checklist import PROMPT_VERSION as CHECKLIST_PV
from agents.trader.checklist import run_checklist
from agents.trader.context import PROMPT_VERSION as CONTEXT_PV
from agents.trader.context import run_context
from agents.trader.entry import PROMPT_VERSION as ENTRY_PV
from agents.trader.entry import run_entry
from agents.trader.risk_plan import PROMPT_VERSION as RISK_PLAN_PV
from agents.trader.risk_plan import run_risk_plan
from agents.trader.setup import PROMPT_VERSION as SETUP_PV
from agents.trader.setup import run_setup
from agents.trader.structure import PROMPT_VERSION as STRUCTURE_PV
from agents.trader.structure import run_structure
from agents.trader.types import TraderBundle, TraderStep
from agents.trader.universe import PROMPT_VERSION as UNIVERSE_PV
from agents.trader.universe import run_universe
from core.activity import BOARD
from core.config import Settings, get_settings
from core.enums import AssessmentKind, InstrumentThesis, NewsCheck, TradeAction
from core.ports import AuditPort, MarketDataPort
from core.redaction import redact_secrets
from core.schemas import NewsAssessment, PipelineResult, TradeCandidate
from strategy.registry import LIVE_STRATEGY_KEY

DESK_VERSION = LIVE_STRATEGY_KEY

# Vendor / data integrity failures — not a market "no setup" decision.
_DATA_FAIL_CODES = frozenset(
    {
        "CONTEXT_ALPACA_ERROR",
        "CONTEXT_INSUFFICIENT_BARS",
        "UNIVERSE_ALPACA_ERROR",
        "UNIVERSE_THIN_HISTORY",
        "STALE_BARS",
        "CHECKLIST_QUOTE_ERROR",
        "CHECKLIST_NO_QUOTE",
        "CHECKLIST_BAD_MID",
        "CHECKLIST_QUOTE_STALE",
        "CHECKLIST_NEWS",
        "ALPACA_KEYS_MISSING",
        "ALPACA_NEWS_ERROR",
        "ALPACA_NEWS_BAD_SHAPE",
        "ENTRY_NO_FEATURES",
        "ENTRY_NO_PRICE",
        "ENTRY_NO_ATR",
        "SETUP_NO_FEATURES",
        "SETUP_NO_CLOSE",
        "SETUP_NO_SMA20",
        "STRUCTURE_NO_D1",
        "RISK_PLAN_NO_PRICE",
        "RISK_PLAN_NO_ATR",
    }
)


_BOARD_IDS = {
    TraderStep.CONTEXT: "context",
    TraderStep.UNIVERSE: "universe",
    TraderStep.STRUCTURE: "structure",
    TraderStep.SETUP: "setup",
    TraderStep.ENTRY: "entry",
    TraderStep.RISK_PLAN: "risk_plan",
    TraderStep.CHECKLIST: "checklist",
}


def _mark(
    step: TraderStep,
    *,
    status: str,
    detail: str,
    symbol: str,
    score: float | None = None,
    filtered_out: bool = False,
) -> None:
    """Log a desk step. ``filtered_out`` = normal no-candidate, not a system fault."""
    aid = _BOARD_IDS[step]
    if filtered_out:
        status = "done"
    BOARD.set_agent(aid, status=status, detail=detail, symbol=symbol, score=score)
    level = "info" if filtered_out or status != "error" else "error"
    BOARD.log(aid, detail, symbol=symbol, level=level)


def _build_candidate(bundle: TraderBundle, *, run_id: UUID) -> TradeCandidate:
    plan = bundle.risk_plan
    tech = bundle.technical
    market = bundle.market
    news = bundle.news
    assert plan is not None and tech is not None and market is not None and news is not None
    entry_bundle = getattr(bundle, "_entry_decision", None)
    reasons = [
        f"desk={DESK_VERSION}",
        *[r for s in bundle.steps for r in s.reasons[:2]],
    ][:12]
    return TradeCandidate(
        symbol=bundle.symbol,
        action=TradeAction.BUY,
        confidence=min(0.95, 0.55 + (tech.score / 200.0)),
        entry=plan.entry,
        stop=plan.stop,
        target=plan.target,
        risk_reward=plan.risk_reward,
        reasons=reasons or ["trader_desk_pass"],
        strategy_version=DESK_VERSION,
        technical_score=tech.score,
        quant_score=tech.score,
        news_label=news.sentiment,
        market_label=market.regime.value,
        pipeline_run_id=run_id,
        exec_timeframe=plan.exec_timeframe,
        thesis=InstrumentThesis.BULLISH,
        setup_type=bundle.setup_type,
        entry_decision=entry_bundle.entry_decision if entry_bundle is not None else None,
        entry_quality=entry_bundle.entry_quality if entry_bundle is not None else None,
        setup_quality=entry_bundle.setup_quality if entry_bundle is not None else None,
        chase_reasons=list(entry_bundle.chase_reasons) if entry_bundle is not None else [],
        entry_zone_low=entry_bundle.entry_zone_low if entry_bundle is not None else None,
        entry_zone_high=entry_bundle.entry_zone_high if entry_bundle is not None else None,
        session_cohort=getattr(getattr(bundle, "_entry_facts", None), "session_cohort", None),
        entry_quality_breakdown=(
            entry_bundle.breakdown.as_dict() if entry_bundle is not None else {}
        ),
        setup_quality_breakdown=(
            entry_bundle.setup_breakdown.as_dict()
            if entry_bundle is not None and entry_bundle.setup_breakdown is not None
            else {}
        ),
    )


async def run_trader_desk(
    symbol: str,
    *,
    market_data: MarketDataPort,
    audit: AuditPort,
    settings: Settings | None = None,
) -> PipelineResult:
    settings = settings or get_settings()
    run_id = uuid4()
    symbol = symbol.upper()
    bundle = TraderBundle(symbol=symbol)
    prompt_versions = {
        "desk": DESK_VERSION,
        "context": CONTEXT_PV,
        "universe": UNIVERSE_PV,
        "structure": STRUCTURE_PV,
        "setup": SETUP_PV,
        "entry": ENTRY_PV,
        "risk_plan": RISK_PLAN_PV,
        "checklist": CHECKLIST_PV,
    }

    await audit.append(
        "TraderDeskStarted",
        "trader_desk",
        {"symbol": symbol, "version": DESK_VERSION},
        pipeline_run_id=run_id,
        entity_type="symbol",
        entity_id=symbol,
    )

    # 1 Context
    _mark(TraderStep.CONTEXT, status="working", detail="SPY regime (Alpaca)", symbol=symbol)
    step = await run_context(bundle, market_data)
    _mark(
        TraderStep.CONTEXT,
        status="done" if step.ok else "error",
        detail=step.detail,
        symbol=symbol,
        score=step.score,
    )
    if not step.ok:
        return _fail(run_id, symbol, bundle, prompt_versions, default_status="no_trade")

    # 2 Universe
    _mark(TraderStep.UNIVERSE, status="working", detail="Liquidity / price", symbol=symbol)
    step = await run_universe(bundle, market_data)
    _mark(
        TraderStep.UNIVERSE,
        status="done" if step.ok else "error",
        detail=step.detail,
        symbol=symbol,
        score=step.score,
    )
    if not step.ok:
        from trading.admission_relaxation import record_funnel

        record_funnel("candidate_rejected")
        return _fail(run_id, symbol, bundle, prompt_versions, default_status="no_candidate")

    # 3 Structure
    _mark(TraderStep.STRUCTURE, status="working", detail="D1 structure", symbol=symbol)
    step = run_structure(bundle)
    _mark(
        TraderStep.STRUCTURE,
        status="done" if step.ok else "error",
        detail=step.detail,
        symbol=symbol,
        score=step.score,
        filtered_out=not step.ok,
    )
    if not step.ok:
        from trading.admission_relaxation import record_funnel

        record_funnel("candidate_rejected")
        return _fail(run_id, symbol, bundle, prompt_versions, default_status="no_candidate")

    # 4 Setup
    _mark(TraderStep.SETUP, status="working", detail="Pullback setup", symbol=symbol)
    step = run_setup(bundle)
    _mark(
        TraderStep.SETUP,
        status="done" if step.ok else "error",
        detail=step.detail,
        symbol=symbol,
        score=step.score,
        filtered_out=not step.ok,
    )
    if not step.ok:
        from trading.admission_relaxation import record_funnel

        record_funnel("candidate_rejected")
        return _fail(run_id, symbol, bundle, prompt_versions, default_status="no_candidate")

    from trading.admission_relaxation import record_funnel

    record_funnel("scanner_candidates")

    # 5 Entry
    _mark(TraderStep.ENTRY, status="working", detail="Entry timing", symbol=symbol)
    step = run_entry(bundle)
    wait_path = step.ok and "ENTRY_WAIT" in step.reasons
    _mark(
        TraderStep.ENTRY,
        status="done" if step.ok else "error",
        detail=step.detail,
        symbol=symbol,
        score=step.score,
        filtered_out=not step.ok,
    )
    if not step.ok:
        return _fail(run_id, symbol, bundle, prompt_versions, default_status="no_trade")

    # 6 Risk plan (geometry for BUY card and WAIT watch alike)
    _mark(TraderStep.RISK_PLAN, status="working", detail="Stop / target / R:R", symbol=symbol)
    step = run_risk_plan(bundle)
    _mark(
        TraderStep.RISK_PLAN,
        status="done" if step.ok else "error",
        detail=step.detail,
        symbol=symbol,
        score=step.score,
    )
    if not step.ok:
        return _fail(run_id, symbol, bundle, prompt_versions, default_status="no_candidate")

    if wait_path:
        # WAIT plans do not need a live BUY checklist; news is re-checked on trigger.
        if bundle.news is None:
            bundle.news = NewsAssessment(
                kind=AssessmentKind.NEWS,
                symbol=symbol,
                sentiment="neutral",
                score=50,
                reasons=["WAIT_PATH_NEWS_AT_TRIGGER"],
                status=NewsCheck.CHECKED,
            )
        entry_bundle = getattr(bundle, "_entry_decision", None)
        # Align desk geometry with the zone wait plan the pipeline will publish.
        if (
            entry_bundle is not None
            and entry_bundle.entry_zone_low is not None
            and entry_bundle.entry_zone_high is not None
        ):
            from dataclasses import replace
            from decimal import Decimal

            from trading.target_model import build_target_plan
            from trading.wait_plan import derive_wait_levels

            wait_levels = derive_wait_levels(entry_bundle)
            tp = build_target_plan(
                entry=wait_levels.entry,
                stop=wait_levels.stop,
                facts=entry_bundle.facts,
                min_rr=2.0,
            )
            bundle._planned = (
                float(wait_levels.entry),
                float(wait_levels.stop),
                float(tp.price),
            )
            if bundle.risk_plan is not None:
                risk = wait_levels.entry - wait_levels.stop
                rr = float((tp.price - wait_levels.entry) / risk) if risk > 0 else 0.0
                bundle.risk_plan = replace(
                    bundle.risk_plan,
                    entry=wait_levels.entry.quantize(Decimal("0.01")),
                    stop=wait_levels.stop.quantize(Decimal("0.01")),
                    target=tp.price.quantize(Decimal("0.01")),
                    risk_reward=rr,
                )
            entry_bundle = entry_bundle.model_copy(
                update={"stop_price": wait_levels.stop, "target": tp}
            )
            bundle._entry_decision = entry_bundle
        candidate = _build_candidate(bundle, run_id=run_id)
        await audit.append(
            "TraderDeskWaitCandidate",
            "trader_desk",
            {
                "symbol": symbol,
                "entry": str(candidate.entry),
                "reasons": candidate.chase_reasons[:4],
            },
            pipeline_run_id=run_id,
            entity_type="symbol",
            entity_id=symbol,
        )
        return PipelineResult(
            pipeline_run_id=run_id,
            symbol=symbol,
            status="completed",
            technical=bundle.technical,
            news=bundle.news,
            market=bundle.market,
            candidate=candidate,
            entry_decision=entry_bundle,
            prompt_versions=prompt_versions,
        )

    # 7 Checklist (quote + Alpaca news) — BUY path only
    _mark(TraderStep.CHECKLIST, status="working", detail="Quote + news", symbol=symbol)
    step = await run_checklist(bundle, market_data, settings=settings)
    _mark(
        TraderStep.CHECKLIST,
        status="done" if step.ok else "error",
        detail=step.detail,
        symbol=symbol,
        score=step.score,
    )
    if not step.ok:
        return _fail(run_id, symbol, bundle, prompt_versions, default_status="no_trade")

    candidate = _build_candidate(bundle, run_id=run_id)
    entry_bundle = getattr(bundle, "_entry_decision", None)

    await audit.append(
        "TraderDeskCandidate",
        "trader_desk",
        {
            "symbol": symbol,
            "steps": [{"step": s.step.value, "ok": s.ok, "detail": s.detail} for s in bundle.steps],
            "entry": str(candidate.entry),
            "stop": str(candidate.stop),
            "target": str(candidate.target),
        },
        pipeline_run_id=run_id,
        entity_type="symbol",
        entity_id=symbol,
    )

    return PipelineResult(
        pipeline_run_id=run_id,
        symbol=symbol,
        status="completed",
        technical=bundle.technical,
        news=bundle.news,
        market=bundle.market,
        candidate=candidate,
        entry_decision=entry_bundle,
        prompt_versions=prompt_versions,
    )


def _fail(
    run_id: UUID,
    symbol: str,
    bundle: TraderBundle,
    prompt_versions: dict[str, str],
    *,
    default_status: str,
) -> PipelineResult:
    failed = bundle.failed
    # WAIT used to be recorded as a failed step; ignore ok steps when looking for errors.
    errors: list[str] = []
    for s in reversed(bundle.steps):
        if not s.ok:
            errors = [redact_secrets(r) for r in s.reasons]
            break
    if not errors:
        errors = [redact_secrets(r) for r in (failed.reasons if failed else ["TRADER_DESK_FAIL"])]
    status = "failed" if any(code in _DATA_FAIL_CODES for code in errors) else default_status
    return PipelineResult(
        pipeline_run_id=run_id,
        symbol=symbol,
        status=status,
        technical=bundle.technical,
        news=bundle.news,
        market=bundle.market,
        entry_decision=getattr(bundle, "_entry_decision", None),
        errors=errors,
        prompt_versions=prompt_versions,
    )
