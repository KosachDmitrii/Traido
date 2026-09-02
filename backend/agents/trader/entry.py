"""Entry agent — buy now vs wait. Uses deterministic timing facts only."""

from __future__ import annotations

from decimal import Decimal

from agents.trader.types import StepResult, TraderBundle, TraderStep
from core.enums import EntryDecision, InstrumentThesis, SessionCohort, Timeframe
from trading.entry_quality import decide_entry
from trading.entry_timing import evaluate_timing
from trading.target_model import build_target_plan

PROMPT_VERSION = "trader.entry@1.1.0"


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
    sma20 = exec_snap.indicators.get("sma_20")
    if isinstance(sma20, (int, float)) and 0 < sma20 <= close:
        planned_entry = float(sma20)
    else:
        planned_entry = float(close)
    planned_stop = planned_entry - 1.5 * atr_f
    supports = exec_snap.support or []
    if supports:
        try:
            nearest = max(float(s) for s in supports if float(s) < planned_entry)
            planned_stop = max(planned_stop, nearest * 0.995)
        except ValueError:
            pass

    entry_d = Decimal(str(round(planned_entry, 4)))
    stop_d = Decimal(str(round(planned_stop, 4)))
    if stop_d >= entry_d:
        result = StepResult(
            step=TraderStep.ENTRY,
            ok=False,
            detail="Invalid stop",
            reasons=["ENTRY_STOP_INVALID"],
            score=0,
        )
        bundle.record(result)
        return result

    prelim_target = float(entry_d + Decimal(2) * (entry_d - stop_d))
    facts = evaluate_timing(
        exec_snap,
        signal_price=float(close),
        planned_entry=float(entry_d),
        planned_stop=float(stop_d),
        planned_target=prelim_target,
        market=bundle.market,
    )
    target_plan = build_target_plan(entry=entry_d, stop=stop_d, facts=facts, min_rr=2.0)
    facts = evaluate_timing(
        exec_snap,
        signal_price=float(close),
        planned_entry=float(entry_d),
        planned_stop=float(stop_d),
        planned_target=float(target_plan.price),
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
        target=target_plan,
        stop_price=float(stop_d),
    )

    bundle._entry_facts = facts  # type: ignore[attr-defined]
    bundle._planned = (
        float(entry_d),
        float(stop_d),
        float(target_plan.price),
    )  # type: ignore[attr-defined]
    bundle._entry_decision = decision  # type: ignore[attr-defined]
    bundle._target_plan = target_plan  # type: ignore[attr-defined]

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
        # Not a hard fail — desk should publish a WAIT card (EntryWatch).
        result = StepResult(
            step=TraderStep.ENTRY,
            ok=True,
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
