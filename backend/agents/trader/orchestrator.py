"""Trader desk orchestrator — one agent per professional step, Alpaca data only.

Order: context → universe → structure → setup → entry → risk_plan → checklist.
Any fail stops the chain. Success yields a TradeCandidate for RiskEngine + desk.
Never places an order.
"""

from __future__ import annotations

from uuid import uuid4

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
from core.enums import InstrumentThesis, SetupType, TradeAction
from core.ports import AuditPort, MarketDataPort
from core.schemas import PipelineResult, TradeCandidate

DESK_VERSION = "trader_desk@1.0.0"


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
    step: TraderStep, *, status: str, detail: str, symbol: str, score: float | None = None
) -> None:
    aid = _BOARD_IDS[step]
    BOARD.set_agent(aid, status=status, detail=detail, symbol=symbol, score=score)
    BOARD.log(aid, detail, symbol=symbol, level="info" if status != "error" else "error")


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
        return _fail(run_id, symbol, bundle, prompt_versions, status="no_trade")

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
        return _fail(run_id, symbol, bundle, prompt_versions, status="no_candidate")

    # 3 Structure
    _mark(TraderStep.STRUCTURE, status="working", detail="D1 structure", symbol=symbol)
    step = run_structure(bundle)
    _mark(
        TraderStep.STRUCTURE,
        status="done" if step.ok else "error",
        detail=step.detail,
        symbol=symbol,
        score=step.score,
    )
    if not step.ok:
        return _fail(run_id, symbol, bundle, prompt_versions, status="no_candidate")

    # 4 Setup
    _mark(TraderStep.SETUP, status="working", detail="Pullback setup", symbol=symbol)
    step = run_setup(bundle)
    _mark(
        TraderStep.SETUP,
        status="done" if step.ok else "error",
        detail=step.detail,
        symbol=symbol,
        score=step.score,
    )
    if not step.ok:
        return _fail(run_id, symbol, bundle, prompt_versions, status="no_candidate")

    # 5 Entry
    _mark(TraderStep.ENTRY, status="working", detail="Entry timing", symbol=symbol)
    step = run_entry(bundle)
    _mark(
        TraderStep.ENTRY,
        status="done" if step.ok else "error",
        detail=step.detail,
        symbol=symbol,
        score=step.score,
    )
    if not step.ok:
        status = "wait_for_entry" if "ENTRY_WAIT" in step.reasons else "no_trade"
        return _fail(run_id, symbol, bundle, prompt_versions, status=status)

    # 6 Risk plan
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
        return _fail(run_id, symbol, bundle, prompt_versions, status="no_candidate")

    # 7 Checklist (quote + Alpaca news)
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
        return _fail(run_id, symbol, bundle, prompt_versions, status="no_trade")

    plan = bundle.risk_plan
    assert plan is not None
    tech = bundle.technical
    assert tech is not None
    market = bundle.market
    assert market is not None
    news = bundle.news
    assert news is not None

    entry_bundle = getattr(bundle, "_entry_decision", None)
    reasons = [
        f"desk={DESK_VERSION}",
        *[r for s in bundle.steps for r in s.reasons[:2]],
    ][:12]

    candidate = TradeCandidate(
        symbol=symbol,
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
        setup_type=SetupType.PULLBACK_CONTINUATION,
        entry_decision=entry_bundle.entry_decision if entry_bundle is not None else None,
        entry_quality=entry_bundle.entry_quality if entry_bundle is not None else None,
        session_cohort=getattr(getattr(bundle, "_entry_facts", None), "session_cohort", None),
    )

    await audit.append(
        "TraderDeskCandidate",
        "trader_desk",
        {
            "symbol": symbol,
            "steps": [{"step": s.step.value, "ok": s.ok, "detail": s.detail} for s in bundle.steps],
            "entry": str(plan.entry),
            "stop": str(plan.stop),
            "target": str(plan.target),
        },
        pipeline_run_id=run_id,
        entity_type="symbol",
        entity_id=symbol,
    )

    return PipelineResult(
        pipeline_run_id=run_id,
        symbol=symbol,
        status="completed",
        technical=tech,
        news=news,
        market=market,
        candidate=candidate,
        entry_decision=entry_bundle,
        prompt_versions=prompt_versions,
    )


def _fail(
    run_id,
    symbol: str,
    bundle: TraderBundle,
    prompt_versions: dict[str, str],
    *,
    status: str,
) -> PipelineResult:
    failed = bundle.failed
    errors = list(failed.reasons) if failed else ["TRADER_DESK_FAIL"]
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
