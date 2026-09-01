"""Technical Agent — scores precomputed FeatureSnapshots (deterministic primary path)."""

from __future__ import annotations

from core.enums import AssessmentKind, Timeframe
from core.schemas import FeatureSnapshot, TechnicalAssessment

PROMPT_VERSION = "technical@0.1.0"


def assess_technical(
    symbol: str,
    features_by_tf: dict[Timeframe, FeatureSnapshot],
) -> TechnicalAssessment:
    primary = features_by_tf.get(Timeframe.D1) or next(iter(features_by_tf.values()))
    ind = primary.indicators
    candles = primary.candlestick_patterns
    charts = primary.chart_patterns

    score = 40
    reasons: list[str] = []

    ema_stack = ind.get("ema50_above_ema200")
    if ema_stack is True:
        score += 20
        reasons.append("EMA50 above EMA200")
    elif ema_stack is False:
        score -= 15
        reasons.append("EMA50 below EMA200")

    structure = charts.get("structure")
    if structure == "uptrend":
        score += 15
        reasons.append("Structure: uptrend")
    elif structure == "downtrend":
        score -= 15
        reasons.append("Structure: downtrend")
    else:
        reasons.append("Structure: range")

    rsi_v = ind.get("rsi_14")
    if isinstance(rsi_v, (int, float)):
        if 40 <= rsi_v <= 60:
            score += 12
            reasons.append(f"RSI {rsi_v:.1f} constructive")
        elif 60 < rsi_v <= 70:
            score += 6
            reasons.append(f"RSI {rsi_v:.1f} elevated but usable")
        elif rsi_v > 75:
            score -= 12
            reasons.append(f"RSI {rsi_v:.1f} overbought")
        elif rsi_v < 30:
            score += 5
            reasons.append(f"RSI {rsi_v:.1f} oversold bounce candidate")

    rvol = ind.get("relative_volume")
    if isinstance(rvol, (int, float)) and rvol >= 1.5:
        score += 8
        reasons.append(f"Relative volume {rvol:.2f}")

    bullish = any(candles.get(k) for k in ("hammer", "bullish_engulfing", "morning_star"))
    bearish = any(candles.get(k) for k in ("shooting_star", "bearish_engulfing", "evening_star"))
    if bullish:
        score += 10
        reasons.append("Bullish candlestick pattern")
    if bearish:
        score -= 10
        reasons.append("Bearish candlestick pattern")

    if charts.get("double_bottom"):
        score += 8
        reasons.append("Double bottom flag")
    if charts.get("double_top"):
        score -= 8
        reasons.append("Double top flag")

    score = max(0, min(100, score))
    trend = "bullish" if score >= 60 else "bearish" if score <= 40 else "neutral"

    tf_summary = {
        tf.value: str(snap.chart_patterns.get("structure") or "n/a")
        for tf, snap in features_by_tf.items()
    }

    return TechnicalAssessment(
        kind=AssessmentKind.TECHNICAL,
        symbol=symbol.upper(),
        trend=trend,
        score=score,
        rsi=float(rsi_v) if isinstance(rsi_v, (int, float)) else None,
        relative_volume=float(rvol) if isinstance(rvol, (int, float)) else None,
        breakout_confirmed=bool(charts.get("higher_highs")),
        support_confirmed=bool(primary.support),
        ema50_above_ema200=ema_stack if isinstance(ema_stack, bool) else None,
        pattern_flags={k: bool(v) for k, v in candles.items()},
        reasons=reasons or ["Insufficient edge from features"],
        timeframe_summary=tf_summary,
    )
