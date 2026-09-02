"""Setup quality — how good is the trade idea, not the current entry price."""

from __future__ import annotations

from core.schemas import EntryTimingFacts, MarketAssessment, SetupQualityBreakdown


def _clamp(n: int) -> int:
    return max(0, min(100, n))


def score_setup_quality(
    facts: EntryTimingFacts,
    *,
    market: MarketAssessment | None = None,
    technical_score: int | None = None,
    news_score: int | None = None,
) -> SetupQualityBreakdown:
    """Deterministic setup score 0–100."""
    if facts.impulse_grade == "A":
        impulse_q = 90
    elif facts.impulse_grade == "B":
        impulse_q = 70
    elif facts.impulse_grade == "C":
        impulse_q = 25
    else:
        impulse_q = 50

    if facts.retracement_pct is not None:
        r = facts.retracement_pct
        if 0.38 <= r <= 0.62:
            retrace_q = 92
        elif 0.30 <= r < 0.38 or 0.62 < r <= 0.78:
            retrace_q = 65
        elif r > 0.786:
            retrace_q = 15
        else:
            retrace_q = 45
    else:
        retrace_q = 50

    if facts.pullback_vol_ratio is not None:
        if facts.pullback_vol_ratio <= 0.75:
            vol = 88
        elif facts.pullback_vol_ratio <= 1.0:
            vol = 62
        else:
            vol = 25
    elif facts.relative_volume is not None:
        vol = 85 if facts.relative_volume >= 1.5 else 65 if facts.relative_volume >= 1.0 else 40
    else:
        vol = 50

    if facts.distance_to_support_pct is None:
        support = 45
    elif facts.distance_to_support_pct <= 1.0:
        support = 80
    elif facts.distance_to_support_pct <= 2.0:
        support = 60
    else:
        support = 35

    market_al = 70
    if market is not None:
        if market.risk_posture == "risk_off" or market.regime.value in {
            "bearish",
            "risk_off",
            "high_volatility",
        }:
            market_al = 25
        elif market.regime.value in {"bullish", "risk_on"}:
            market_al = 90
        else:
            market_al = 60

    catalyst = 70
    if news_score is not None:
        catalyst = _clamp(news_score)

    liquidity = 70
    if facts.relative_volume is not None:
        liquidity = 85 if facts.relative_volume >= 1.0 else 55

    trend = 50
    if technical_score is not None:
        trend = _clamp(technical_score)
    elif facts.impulse_grade in {"A", "B"}:
        trend = 75

    if facts.pullback_index is not None and facts.pullback_index >= 3:
        impulse_q = min(impulse_q, 35)

    return SetupQualityBreakdown(
        trend_structure=_clamp(trend),
        impulse_quality=_clamp(impulse_q),
        retracement_structure=_clamp(retrace_q),
        volume_participation=_clamp(vol),
        support_structure=_clamp(support),
        market_alignment=_clamp(market_al),
        catalyst=_clamp(catalyst),
        liquidity=_clamp(liquidity),
    )
