"""Entry agent — buy now vs wait. Uses deterministic timing facts only."""

from __future__ import annotations

from decimal import Decimal

from agents.trader.types import StepResult, TraderBundle, TraderStep
from core.enums import EntryDecision, InstrumentThesis, SessionCohort, Timeframe
from trading.current_entry_plan import current_entry_is_eligible, current_entry_zone
from trading.entry_policy import get_candidate_thresholds
from trading.entry_quality import decide_entry
from trading.entry_timing import evaluate_timing
from trading.stop_validation import validate_stop
from trading.target_model import build_target_plan
from trading.target_validation import validate_target

PROMPT_VERSION = "trader.entry@1.2.0"


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
    if not isinstance(atr, (int, float)) or atr <= 0:
        result = StepResult(
            step=TraderStep.ENTRY,
            ok=False,
            detail="ATR missing",
            reasons=["ENTRY_NO_ATR"],
            score=0,
        )
        bundle.record(result)
        return result
    atr_f = float(atr)
    sma20 = exec_snap.indicators.get("sma_20")
    sma20_f = float(sma20) if isinstance(sma20, (int, float)) else None
    # Current-entry geometry is candidate discovery, so it stays fixed at the
    # Medium policy. The operator slider is applied later by ``decide_entry``
    # only to the final buy confirmation.
    thresholds = get_candidate_thresholds()
    use_current_entry = current_entry_is_eligible(
        price=float(close),
        sma20=sma20_f,
        atr=atr_f,
        thresholds=thresholds,
    )
    if use_current_entry:
        planned_entry = float(close)
    elif sma20_f is not None and 0 < sma20_f <= close:
        planned_entry = sma20_f
    else:
        planned_entry = float(close)
    planned_stop = planned_entry - 1.5 * atr_f
    supports = exec_snap.support or []
    structural_support: float | None = None
    if supports:
        try:
            structural_support = max(float(s) for s in supports if float(s) < planned_entry)
            # A long stop must sit below the level that invalidates the thesis.
            # Choosing the higher of the ATR stop and support left the stop above
            # support in production, so admission correctly classified it as
            # ATR_ONLY_STOP. ATR may widen a structural stop, never replace it.
            planned_stop = min(planned_stop, structural_support * 0.995)
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

    # BUY_NOW is a promise that the proposed geometry is admissible, not just
    # that timing scores are high. Keep invalid current geometry as an
    # actionable pullback WAIT instead of publishing a BUY candidate that the
    # next layer must immediately destroy.
    if decision.entry_decision is EntryDecision.BUY_NOW:
        stop_check = validate_stop(
            entry=entry_d,
            stop=stop_d,
            facts=facts,
            stop_model="support" if structural_support is not None else None,
            structural_source="nearest_support" if structural_support is not None else None,
            structural_level=structural_support,
        )
        target_check = validate_target(
            entry=entry_d,
            target=target_plan.price,
            target_plan=target_plan,
        )
        geometry_reasons = [*stop_check.reason_codes, *target_check.reason_codes]
        if not stop_check.valid or not target_check.valid:
            decision = decision.model_copy(
                update={
                    "entry_decision": EntryDecision.WAIT_FOR_ENTRY,
                    "reasons": [
                        *decision.reasons,
                        "CURRENT_GEOMETRY_NOT_ADMISSIBLE",
                        *geometry_reasons,
                    ],
                }
            )

    planned_at_current = abs(planned_entry - float(close)) <= max(0.0001, float(close) * 1e-6)
    if planned_at_current and decision.entry_decision is EntryDecision.BUY_NOW:
        zone_low, zone_high = current_entry_zone(
            price=float(close),
            atr=atr_f,
            thresholds=thresholds,
        )
        decision = decision.model_copy(
            update={
                "entry_zone_low": zone_low,
                "entry_zone_high": zone_high,
                "reasons": [
                    *decision.reasons,
                    "CURRENT_ENTRY_NEAR_SMA20"
                    if use_current_entry
                    else "CURRENT_ENTRY_AT_OR_BELOW_SMA20",
                ],
            }
        )

    bundle._entry_facts = facts
    bundle._planned = (
        float(entry_d),
        float(stop_d),
        float(target_plan.price),
    )
    bundle._entry_decision = decision
    bundle._target_plan = target_plan

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
