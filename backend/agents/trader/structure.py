"""Structure agent — multi-TF HTF confluence (D1 + 4H when present)."""

from __future__ import annotations

from agents.trader.policy import structure_ok, trader_gates_for
from agents.trader.types import StepResult, TraderBundle, TraderStep
from core.enums import AssessmentKind, Timeframe
from core.schemas import TechnicalAssessment

PROMPT_VERSION = "trader.structure@1.2.0"


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
    tf_summary: dict[str, str] = {}

    if ema_ok:
        score += 20
        reasons.append("D1 EMA50>EMA200")
    else:
        reasons.append("D1 EMA stack not bullish")

    if structure == "uptrend":
        score += 20
        reasons.append("D1 uptrend")
        tf_summary["D1"] = "bullish"
    elif structure == "downtrend":
        score -= 20
        reasons.append("D1 downtrend")
        tf_summary["D1"] = "bearish"
    else:
        reasons.append(f"D1 structure={structure}")
        tf_summary["D1"] = "neutral"

    # Stage 8: 4H must not contradict D1 when present.
    h4 = bundle.features.get(Timeframe.H4)
    h4_ok = True
    if h4 is not None:
        h4_structure = h4.chart_patterns.get("structure")
        h4_ema = h4.indicators.get("ema50_above_ema200") is True
        if h4_structure == "downtrend":
            h4_ok = False
            score -= 15
            reasons.append("H4 downtrend contradicts")
            tf_summary["H4"] = "bearish"
        elif h4_structure == "uptrend" or h4_ema:
            score += 15
            reasons.append("H4 constructive")
            tf_summary["H4"] = "bullish"
        else:
            reasons.append(f"H4 structure={h4_structure}")
            tf_summary["H4"] = "neutral"

    ok, gate_reasons = structure_ok(structure=structure, ema_ok=ema_ok, policy=policy)
    if not h4_ok:
        ok = False
        gate_reasons = [*gate_reasons, "STRUCTURE_H4_CONFLICT"]
    reasons = [*reasons, *[r for r in gate_reasons if r not in reasons]]
    score = max(0, min(100, score))
    trend = "bullish" if ok else "bearish" if structure == "downtrend" else "neutral"

    breakout = bool(ind.get("pa_breakout")) or bool(d1.chart_patterns.get("triangle_ascending"))
    rsi_v = ind.get("rsi_14")
    rvol = ind.get("relative_volume")
    pattern_flags = {
        "structure_uptrend": structure == "uptrend",
        "breakout": breakout,
        "triangle_ascending": bool(d1.chart_patterns.get("triangle_ascending")),
        "flag_bull": bool(d1.chart_patterns.get("flag_bull")),
        "inv_head_shoulders": bool(d1.chart_patterns.get("inv_head_shoulders")),
    }
    bundle.technical = TechnicalAssessment(
        kind=AssessmentKind.TECHNICAL,
        symbol=bundle.symbol,
        trend=trend,
        score=score,
        rsi=float(rsi_v) if isinstance(rsi_v, (int, float)) else None,
        relative_volume=float(rvol) if isinstance(rvol, (int, float)) else None,
        breakout_confirmed=breakout and ok,
        support_confirmed=structure == "uptrend",
        ema50_above_ema200=ema_ok,
        pattern_flags=pattern_flags,
        reasons=reasons,
        timeframe_summary=tf_summary or {"D1": trend},
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
