"""Setup agent — pullback / breakout / retest / gap continuation (Stage 8)."""

from __future__ import annotations

from agents.trader.policy import trader_gates_for
from agents.trader.types import StepResult, TraderBundle, TraderStep
from core.enums import SetupType, Timeframe

PROMPT_VERSION = "trader.setup@1.2.0"


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def run_setup(bundle: TraderBundle) -> StepResult:
    d1 = bundle.features.get(Timeframe.D1)
    exec_tf = bundle.features.get(Timeframe.H1) or d1
    m15 = bundle.features.get(Timeframe.M15)
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

    policy = trader_gates_for()
    close = _num(exec_tf.indicators.get("close"))
    sma20 = _num(exec_tf.indicators.get("sma_20"))
    rsi_v = _num(exec_tf.indicators.get("rsi_14"))
    reasons: list[str] = [f"policy_a={policy.aggressiveness}"]
    setup_kind = SetupType.PULLBACK_CONTINUATION

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

    # Prefer explicit price-action setups when HTF is constructive.
    pa_breakout = bool(exec_tf.indicators.get("pa_breakout") or d1.indicators.get("pa_breakout"))
    pa_retest = bool(exec_tf.indicators.get("pa_retest"))
    pa_gap_up = bool(exec_tf.indicators.get("pa_gap_up") or d1.indicators.get("pa_gap_up"))

    score = 45
    if pa_breakout and bundle.technical and bundle.technical.trend == "bullish":
        setup_kind = SetupType.BREAKOUT_CONTINUATION
        score += 30
        reasons.append("setup=breakout_continuation")
    elif pa_retest and bundle.technical and bundle.technical.trend == "bullish":
        setup_kind = SetupType.BREAKOUT_CONTINUATION
        score += 28
        reasons.append("setup=retest_continuation")
    elif pa_gap_up and bundle.technical and bundle.technical.trend == "bullish":
        setup_kind = SetupType.GAP_CONTINUATION
        score += 25
        reasons.append("setup=gap_continuation")
    else:
        reasons.append("setup=pullback_continuation")
        if sma20 is not None and sma20 > 0:
            dist = abs(close - sma20) / sma20
            chase_cap = policy.chase_ext_frac
            if dist <= policy.near_sma_frac:
                score += 25
                reasons.append(f"near SMA20 ({dist * 100:.1f}%)")
            elif close > sma20 * (1.0 + chase_cap):
                result = StepResult(
                    step=TraderStep.SETUP,
                    ok=False,
                    detail="Chase — extended",
                    reasons=[
                        *reasons,
                        "SETUP_CHASE",
                        f"dist={dist * 100:.1f}%",
                        f"cap={chase_cap * 100:.1f}%",
                    ],
                    score=20,
                )
                bundle.record(result)
                return result
            elif close >= sma20:
                score += 10
                reasons.append("above SMA20, not extended")
            elif policy.allow_below_sma and dist <= policy.near_sma_frac * 2.0:
                score += 5
                reasons.append(f"shallow below SMA20 ({dist * 100:.1f}%)")
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
            result = StepResult(
                step=TraderStep.SETUP,
                ok=False,
                detail="SMA20 missing",
                reasons=[*reasons, "SETUP_NO_SMA20"],
                score=0,
            )
            bundle.record(result)
            return result

    if rsi_v is not None:
        if rsi_v >= policy.rsi_overbought:
            result = StepResult(
                step=TraderStep.SETUP,
                ok=False,
                detail="RSI overbought",
                reasons=[
                    *reasons,
                    "SETUP_RSI_HIGH",
                    f"rsi={rsi_v:.0f}",
                    f"cap={policy.rsi_overbought:.0f}",
                ],
                score=20,
            )
            bundle.record(result)
            return result
        if 35 <= rsi_v <= 65:
            score += 10
            reasons.append(f"RSI {rsi_v:.0f} constructive")
        else:
            reasons.append(f"RSI {rsi_v:.0f}")

    # 15m alignment is a bonus, never a hard reject when missing.
    if m15 is not None:
        m15_rsi = _num(m15.indicators.get("rsi_14"))
        if m15_rsi is not None and m15_rsi < 75:
            score += 5
            reasons.append("M15 not overbought")
        elif m15_rsi is not None:
            reasons.append("M15 hot")

    bundle.setup_type = setup_kind
    score = max(0, min(100, score))
    result = StepResult(
        step=TraderStep.SETUP,
        ok=True,
        detail=setup_kind.value,
        reasons=reasons,
        score=score,
    )
    bundle.record(result)
    return result
