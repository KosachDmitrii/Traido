"""Risk-plan agent — stop, target, R:R from structure. Proposal only."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from agents.trader.types import RiskPlan, StepResult, TraderBundle, TraderStep
from core.enums import Timeframe

PROMPT_VERSION = "trader.risk_plan@1.0.0"
MIN_RR = 2.0
# Display rounds to 2dp; accept float dust so "2.00" is not rejected as < 2.0.
_RR_EPS = 1e-6


def _q(x: float) -> Decimal:
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def run_risk_plan(bundle: TraderBundle) -> StepResult:
    planned = getattr(bundle, "_planned", None)
    exec_tf = Timeframe.H1 if Timeframe.H1 in bundle.features else Timeframe.D1
    snap = bundle.features.get(exec_tf)
    if planned is None or snap is None:
        # fallback from close / ATR
        close = snap.indicators.get("close") if snap else None
        atr = snap.indicators.get("atr_14") if snap else None
        if not isinstance(close, (int, float)) or close <= 0:
            result = StepResult(
                step=TraderStep.RISK_PLAN,
                ok=False,
                detail="Cannot size geometry",
                reasons=["RISK_PLAN_NO_PRICE"],
                score=0,
            )
            bundle.record(result)
            return result
        atr_f = float(atr) if isinstance(atr, (int, float)) and atr > 0 else float(close) * 0.02
        planned = (float(close), float(close) - 1.5 * atr_f, float(close) + 3.0 * atr_f)

    entry_f, stop_f, target_f = planned
    if stop_f >= entry_f:
        result = StepResult(
            step=TraderStep.RISK_PLAN,
            ok=False,
            detail="Stop above entry",
            reasons=["RISK_PLAN_STOP_INVALID"],
            score=0,
        )
        bundle.record(result)
        return result

    risk = entry_f - stop_f
    reward = target_f - entry_f
    rr = reward / risk if risk > 0 else 0.0
    reasons = [
        f"entry={entry_f:.2f}",
        f"stop={stop_f:.2f}",
        f"target={target_f:.2f}",
        f"rr={rr:.2f}",
    ]

    if rr + _RR_EPS < MIN_RR:
        result = StepResult(
            step=TraderStep.RISK_PLAN,
            ok=False,
            detail=f"R:R {rr:.2f} < {MIN_RR:g}",
            reasons=[*reasons, "RISK_PLAN_RR_LOW"],
            score=25,
        )
        bundle.record(result)
        return result

    bundle.risk_plan = RiskPlan(
        entry=_q(entry_f),
        stop=_q(stop_f),
        target=_q(target_f),
        risk_reward=float(rr),
        exec_timeframe=exec_tf,
        reasons=reasons,
    )
    result = StepResult(
        step=TraderStep.RISK_PLAN,
        ok=True,
        detail=f"R:R {rr:.2f}",
        reasons=reasons,
        score=75,
    )
    bundle.record(result)
    return result
