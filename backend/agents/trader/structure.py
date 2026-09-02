"""Structure agent — higher-timeframe trend (D1), floors from entry aggressiveness."""

from __future__ import annotations

from agents.trader.policy import structure_ok, trader_gates_for
from agents.trader.types import StepResult, TraderBundle, TraderStep
from core.enums import AssessmentKind, Timeframe
from core.schemas import TechnicalAssessment

PROMPT_VERSION = "trader.structure@1.1.0"


def run_structure(bundle: TraderBundle) -> StepResult:
    d1 = bundle.features.get(Timeframe.D1)
    if d1 is None:
        result = StepResult(
            step=TraderStep.STRUCTURE,
            ok=False,
            detail="No D1 features",
            reasons=["STRUCTURE_NO_D1"],
            score=0,
        )
        bundle.record(result)
        return result

    policy = trader_gates_for()
    ind = d1.indicators
    structure = d1.chart_patterns.get("structure")
    ema_ok = ind.get("ema50_above_ema200") is True
    reasons: list[str] = []
    score = 40

    if ema_ok:
        score += 25
        reasons.append("EMA50>EMA200")
    else:
        reasons.append("EMA stack not bullish")

    if structure == "uptrend":
        score += 25
        reasons.append("D1 uptrend")
    elif structure == "downtrend":
        score -= 20
        reasons.append("D1 downtrend")
    else:
        reasons.append(f"D1 structure={structure}")

    ok, gate_reasons = structure_ok(structure=structure, ema_ok=ema_ok, policy=policy)
    reasons = [*reasons, *[r for r in gate_reasons if r not in reasons]]
    score = max(0, min(100, score))
    trend = "bullish" if ok else "bearish" if structure == "downtrend" else "neutral"

    rsi_v = ind.get("rsi_14")
    rvol = ind.get("relative_volume")
    bundle.technical = TechnicalAssessment(
        kind=AssessmentKind.TECHNICAL,
        symbol=bundle.symbol,
        trend=trend,
        score=score,
        rsi=float(rsi_v) if isinstance(rsi_v, (int, float)) else None,
        relative_volume=float(rvol) if isinstance(rvol, (int, float)) else None,
        breakout_confirmed=False,
        support_confirmed=structure == "uptrend",
        ema50_above_ema200=ema_ok,
        pattern_flags={"structure_uptrend": structure == "uptrend"},
        reasons=reasons,
        timeframe_summary={"D1": trend},
    )

    result = StepResult(
        step=TraderStep.STRUCTURE,
        ok=ok,
        detail=trend if ok else "HTF not long",
        reasons=reasons if ok else [*reasons, "STRUCTURE_REJECT"],
        score=score,
    )
    bundle.record(result)
    return result
