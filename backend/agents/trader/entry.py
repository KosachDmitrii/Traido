"""Entry agent — buy now vs wait. Uses deterministic timing facts only."""

from __future__ import annotations

from agents.trader.types import StepResult, TraderBundle, TraderStep
from core.enums import EntryDecision, InstrumentThesis, SessionCohort, Timeframe
from trading.entry_quality import decide_entry
from trading.entry_timing import evaluate_timing

PROMPT_VERSION = "trader.entry@1.0.0"


def run_entry(bundle: TraderBundle) -> StepResult:
    exec_snap = bundle.features.get(Timeframe.H1) or bundle.features.get(Timeframe.D1)
    if exec_snap is None:
        result = StepResult(
            step=TraderStep.ENTRY,
            ok=False,
            detail="No exec snapshot",
            reasons=["ENTRY_NO_FEATURES"],
            score=0,
        )
        bundle.record(result)
        return result

    close = exec_snap.indicators.get("close")
    if not isinstance(close, (int, float)) or close <= 0:
        result = StepResult(
            step=TraderStep.ENTRY,
            ok=False,
            detail="No price",
            reasons=["ENTRY_NO_PRICE"],
            score=0,
        )
        bundle.record(result)
        return result

    atr = exec_snap.indicators.get("atr_14")
    atr_f = float(atr) if isinstance(atr, (int, float)) and atr > 0 else float(close) * 0.02
    planned_entry = float(close)
    planned_stop = planned_entry - 1.5 * atr_f
    planned_target = planned_entry + 3.0 * atr_f

    facts = evaluate_timing(
        exec_snap,
        signal_price=planned_entry,
        planned_entry=planned_entry,
        planned_stop=planned_stop,
        planned_target=planned_target,
        market=bundle.market,
    )
    tech_score = bundle.technical.score if bundle.technical else None
    news_score = bundle.news.score if bundle.news else None
    decision = decide_entry(
        InstrumentThesis.BULLISH,
        facts,
        market=bundle.market,
        technical_score=tech_score,
        news_score=news_score,
        stop_price=planned_stop,
    )

    bundle._entry_facts = facts  # type: ignore[attr-defined]
    bundle._planned = (planned_entry, planned_stop, planned_target)  # type: ignore[attr-defined]
    bundle._entry_decision = decision  # type: ignore[attr-defined]

    if facts.session_cohort is not SessionCohort.RTH:
        result = StepResult(
            step=TraderStep.ENTRY,
            ok=False,
            detail=f"session={facts.session_cohort.value}",
            reasons=["ENTRY_NOT_RTH", facts.session_cohort.value],
            score=20,
        )
        bundle.record(result)
        return result

    reason_vals = [str(r) for r in decision.reasons][:6]

    if decision.entry_decision is EntryDecision.BUY_NOW:
        result = StepResult(
            step=TraderStep.ENTRY,
            ok=True,
            detail="BUY_NOW",
            reasons=reason_vals or ["ENTRY_BUY_NOW"],
            score=80,
        )
        bundle.record(result)
        return result

    if decision.entry_decision is EntryDecision.WAIT_FOR_ENTRY:
        result = StepResult(
            step=TraderStep.ENTRY,
            ok=False,
            detail="WAIT",
            reasons=["ENTRY_WAIT", *reason_vals[:4]],
            score=40,
        )
        bundle.record(result)
        return result

    result = StepResult(
        step=TraderStep.ENTRY,
        ok=False,
        detail="NO_TRADE",
        reasons=["ENTRY_NO_TRADE", *reason_vals[:4]],
        score=15,
    )
    bundle.record(result)
    return result
