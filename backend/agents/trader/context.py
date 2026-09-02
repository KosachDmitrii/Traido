"""Context agent — market risk-on/off from Alpaca benchmark bars (SPY).

No FRED. No macro vendor. One job: is the tape tradable for longs?
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agents.trader.types import StepResult, TraderBundle, TraderStep
from core.enums import AssessmentKind, MarketRegimeLabel, Timeframe
from core.ports import MarketDataPort
from core.schemas import MarketAssessment
from quant.engine import compute_features

PROMPT_VERSION = "trader.context@1.0.0"
BENCHMARK = "SPY"


async def run_context(bundle: TraderBundle, md: MarketDataPort) -> StepResult:
    end = datetime.now(UTC)
    start = end - timedelta(days=400)
    try:
        bars = await md.get_bars(BENCHMARK, Timeframe.D1, start, end)
    except Exception as exc:  # noqa: BLE001
        result = StepResult(
            step=TraderStep.CONTEXT,
            ok=False,
            detail="Alpaca benchmark failed",
            reasons=["CONTEXT_ALPACA_ERROR", str(exc)[:120]],
            score=0,
        )
        bundle.record(result)
        return result

    if len(bars) < 60:
        result = StepResult(
            step=TraderStep.CONTEXT,
            ok=False,
            detail="Insufficient SPY history",
            reasons=["CONTEXT_INSUFFICIENT_BARS"],
            score=0,
        )
        bundle.record(result)
        return result

    snap = compute_features(BENCHMARK, Timeframe.D1, bars)
    ind = snap.indicators
    structure = snap.chart_patterns.get("structure")
    ema_ok = ind.get("ema50_above_ema200") is True
    atr = ind.get("atr_14")
    close = ind.get("close")

    score = 50
    reasons: list[str] = [f"benchmark={BENCHMARK}"]
    regime = MarketRegimeLabel.NEUTRAL
    posture = "neutral"

    if ema_ok and structure == "uptrend":
        score = 72
        regime = MarketRegimeLabel.BULLISH
        posture = "risk_on"
        reasons.append("SPY uptrend · EMA50>EMA200")
    elif structure == "downtrend" or ind.get("ema50_above_ema200") is False:
        score = 25
        regime = MarketRegimeLabel.BEARISH
        posture = "risk_off"
        reasons.append("SPY downtrend / EMA stack broken")
    else:
        reasons.append("SPY range / mixed")

    if isinstance(atr, (int, float)) and isinstance(close, (int, float)) and close > 0:
        atr_pct = float(atr) / float(close)
        # ~3.5%+ daily ATR on SPY is genuinely hostile; 2.5% was blocking quiet days.
        if atr_pct >= 0.035:
            score = min(score, 35)
            regime = MarketRegimeLabel.HIGH_VOLATILITY
            posture = "risk_off"
            reasons.append(f"High vol ATR {atr_pct * 100:.1f}% of price")

    # Neutral / mixed tape is allowed through — later agents still veto the name.
    # Only hostile regimes stop the whole desk (bear / risk-off / high vol).
    blocked = posture == "risk_off" or regime in {
        MarketRegimeLabel.BEARISH,
        MarketRegimeLabel.RISK_OFF,
        MarketRegimeLabel.HIGH_VOLATILITY,
    }
    ok = not blocked

    bundle.market = MarketAssessment(
        kind=AssessmentKind.MARKET,
        regime=regime,
        score=score,
        risk_posture=posture,
        reasons=reasons,
        macro_notes=[f"source=alpaca:{BENCHMARK}"],
        evaluated_at=datetime.now(UTC),
        benchmark=BENCHMARK,
    )
    result = StepResult(
        step=TraderStep.CONTEXT,
        ok=ok,
        detail=posture if ok else f"blocked · {regime.value}",
        reasons=reasons if ok else [*reasons, "CONTEXT_NOT_TRADABLE"],
        score=score,
    )
    bundle.record(result)
    return result
