"""Setup agent — pullback-continuation only. One pattern, clear yes/no."""

from __future__ import annotations

from agents.trader.types import StepResult, TraderBundle, TraderStep
from core.enums import Timeframe

PROMPT_VERSION = "trader.setup@1.0.0"


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def run_setup(bundle: TraderBundle) -> StepResult:
    d1 = bundle.features.get(Timeframe.D1)
    exec_tf = bundle.features.get(Timeframe.H1) or d1
    if exec_tf is None or d1 is None:
        result = StepResult(
            step=TraderStep.SETUP,
            ok=False,
            detail="No exec features",
            reasons=["SETUP_NO_FEATURES"],
            score=0,
        )
        bundle.record(result)
        return result

    close = _num(exec_tf.indicators.get("close"))
    sma20 = _num(exec_tf.indicators.get("sma_20"))
    rsi_v = _num(exec_tf.indicators.get("rsi_14"))
    reasons: list[str] = ["setup=pullback_continuation"]

    if close is None:
        result = StepResult(
            step=TraderStep.SETUP,
            ok=False,
            detail="No close",
            reasons=["SETUP_NO_CLOSE"],
            score=0,
        )
        bundle.record(result)
        return result

    score = 50
    if sma20 is not None and sma20 > 0:
        dist = abs(close - sma20) / sma20
        if dist <= 0.025:
            score += 25
            reasons.append(f"near SMA20 ({dist * 100:.1f}%)")
        elif close > sma20 * 1.04:
            result = StepResult(
                step=TraderStep.SETUP,
                ok=False,
                detail="Chase — extended",
                reasons=[*reasons, "SETUP_CHASE", f"dist={dist * 100:.1f}%"],
                score=20,
            )
            bundle.record(result)
            return result
        elif close >= sma20:
            score += 10
            reasons.append("above SMA20, not extended")
        else:
            result = StepResult(
                step=TraderStep.SETUP,
                ok=False,
                detail="Below SMA20",
                reasons=[*reasons, "SETUP_BELOW_SMA20"],
                score=25,
            )
            bundle.record(result)
            return result
    else:
        reasons.append("SMA20 missing — soft pass on close only")

    if rsi_v is not None:
        if rsi_v >= 72:
            result = StepResult(
                step=TraderStep.SETUP,
                ok=False,
                detail="RSI overbought",
                reasons=[*reasons, "SETUP_RSI_HIGH", f"rsi={rsi_v:.0f}"],
                score=20,
            )
            bundle.record(result)
            return result
        if 35 <= rsi_v <= 65:
            score += 15
            reasons.append(f"RSI {rsi_v:.0f} constructive")
        else:
            reasons.append(f"RSI {rsi_v:.0f}")

    score = max(0, min(100, score))
    result = StepResult(
        step=TraderStep.SETUP,
        ok=True,
        detail="pullback continuation",
        reasons=reasons,
        score=score,
    )
    bundle.record(result)
    return result
