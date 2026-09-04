"""Strategy Agent — multi-TF confluence + F3 entry timing / adaptive target.

Thesis (bullish confluence) is separated from entry decision (BUY_NOW /
WAIT_FOR_ENTRY / NO_TRADE). Fixed 2R remains a target *candidate*, not the law.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from core.enums import InstrumentThesis, SetupType, Timeframe, TradeAction
from core.schemas import (
    EntryDecisionBundle,
    FeatureSnapshot,
    MarketAssessment,
    NewsAssessment,
    TechnicalAssessment,
    TradeCandidate,
)
from trading.entry_quality import decide_entry
from trading.entry_timing import evaluate_timing
from trading.target_model import build_target_plan

# Registered in strategy.registry (Stage 8). Do not change parameters without a new tag.
from strategy.registry import LIVE_STRATEGY_KEY as STRATEGY_VERSION

MIN_TECHNICAL = 68
MIN_OVERALL = 70
MIN_RISK_REWARD = 2.0
"""Reference 2R used as one TargetModel candidate (not blindly accepted)."""


def _ind(snap: FeatureSnapshot, key: str) -> float | int | bool | str | None:
    return snap.indicators.get(key)


def _confluence(
    features_by_tf: dict[Timeframe, FeatureSnapshot],
) -> tuple[bool, list[str], FeatureSnapshot]:
    """Require D1 trend + H1 (or primary) constructive pullback."""
    reasons: list[str] = []
    d1 = features_by_tf.get(Timeframe.D1)
    h1 = features_by_tf.get(Timeframe.H1)
    primary = d1 or h1 or next(iter(features_by_tf.values()))

    if d1 is None:
        reasons.append("No D1 features — skip")
        return False, reasons, primary

    ema_ok = _ind(d1, "ema50_above_ema200") is True
    structure = d1.chart_patterns.get("structure")
    struct_ok = structure in {"uptrend", "range"} and structure != "downtrend"
    if ema_ok:
        reasons.append("D1 EMA50>EMA200")
    else:
        reasons.append("D1 trend filter failed")
    if structure == "uptrend":
        reasons.append("D1 structure uptrend")
        struct_ok = True
    elif structure == "downtrend":
        reasons.append("D1 structure downtrend — reject")
        struct_ok = False

    htf_ok = ema_ok and struct_ok
    if not htf_ok:
        return False, reasons, primary

    exec_tf = h1 or primary
    close = _ind(exec_tf, "close")
    sma20 = _ind(exec_tf, "sma_20")
    rsi_v = _ind(exec_tf, "rsi_14")
    if not isinstance(close, (int, float)):
        return False, reasons + ["No close"], primary

    if isinstance(sma20, (int, float)) and sma20 > 0:
        dist = abs(close - sma20) / sma20
        if dist <= 0.025:
            reasons.append(f"Pullback near SMA20 ({dist * 100:.1f}%)")
        elif close > sma20 * 1.04:
            reasons.append("Extended >4% above SMA20 — reject chase")
            return False, reasons, exec_tf
        else:
            reasons.append("Above SMA20 with acceptable extension")
    else:
        reasons.append("SMA20 unavailable — use close entry")

    if isinstance(rsi_v, (int, float)):
        if rsi_v >= 72:
            reasons.append(f"RSI {rsi_v:.0f} overbought — reject")
            return False, reasons, exec_tf
        if 35 <= rsi_v <= 65:
            reasons.append(f"RSI {rsi_v:.0f} constructive")
        else:
            reasons.append(f"RSI {rsi_v:.0f}")

    return True, reasons, exec_tf


def propose_trade(
    symbol: str,
    technical: TechnicalAssessment,
    news: NewsAssessment,
    market: MarketAssessment,
    features_by_tf: dict[Timeframe, FeatureSnapshot],
    *,
    pipeline_run_id: UUID | None = None,
    min_technical: int = MIN_TECHNICAL,
    min_overall: int = MIN_OVERALL,
    min_rr: float = MIN_RISK_REWARD,
) -> TradeCandidate | None:
    """Build geometry when thesis is bullish. EntryTiming may still say WAIT.

    Returns None only when there is no bullish thesis / scores fail. A WAIT
    decision still returns a TradeCandidate so the pipeline can open an
    EntryWatch and record shadow OLD vs NEW.
    """
    candidate, _bundle = propose_with_entry_timing(
        symbol,
        technical,
        news,
        market,
        features_by_tf,
        pipeline_run_id=pipeline_run_id,
        min_technical=min_technical,
        min_overall=min_overall,
        min_rr=min_rr,
    )
    return candidate


def propose_with_entry_timing(
    symbol: str,
    technical: TechnicalAssessment,
    news: NewsAssessment,
    market: MarketAssessment,
    features_by_tf: dict[Timeframe, FeatureSnapshot],
    *,
    pipeline_run_id: UUID | None = None,
    min_technical: int = MIN_TECHNICAL,
    min_overall: int = MIN_OVERALL,
    min_rr: float = MIN_RISK_REWARD,
) -> tuple[TradeCandidate | None, EntryDecisionBundle | None]:
    ok, conf_reasons, exec_snap = _confluence(features_by_tf)
    if not ok:
        return None, None

    close = _ind(exec_snap, "close")
    atr_v = _ind(exec_snap, "atr_14")
    sma20 = _ind(exec_snap, "sma_20")
    if not isinstance(close, (int, float)) or close <= 0:
        return None, None
    if not isinstance(atr_v, (int, float)) or atr_v <= 0:
        atr_v = close * 0.02

    quant_score = technical.score
    stub_news = any("not configured" in r.lower() for r in news.reasons)
    stub_market = any("not configured" in r.lower() for r in market.reasons)
    if stub_news and stub_market:
        overall = technical.score
    else:
        overall = round(0.55 * technical.score + 0.2 * news.score + 0.25 * market.score)

    if technical.score < min_technical:
        return None, None
    if news.sentiment == "negative" and news.score < 40:
        return None, None
    if market.risk_posture == "risk_off" and market.score < 45:
        return None, None
    if overall < min_overall:
        return None, None

    # Planned entry for geometry / OLD policy (may differ from live close).
    if isinstance(sma20, (int, float)) and 0 < sma20 <= close:
        entry_f = float(sma20)
        conf_reasons.append("Entry at SMA20 pullback")
    else:
        entry_f = float(close)
        conf_reasons.append("Entry at close")

    stop_f = entry_f - 1.5 * float(atr_v)
    supports = exec_snap.support or []
    if supports:
        try:
            nearest = max(s for s in supports if s < entry_f)
            stop_f = max(stop_f, float(nearest) * 0.995)
            conf_reasons.append(f"Stop anchored near support {nearest:.2f}")
        except ValueError:
            pass

    entry = Decimal(str(round(entry_f, 4)))
    stop = Decimal(str(round(stop_f, 4)))
    risk = entry - stop
    if risk <= 0:
        return None, None

    signal_price = Decimal(str(round(float(close), 4)))
    facts = evaluate_timing(
        exec_snap,
        signal_price=float(close),
        planned_entry=float(entry),
        planned_stop=float(stop),
        planned_target=float(entry + Decimal(str(min_rr)) * risk),
        market=market,
    )
    from trading.historical_mfe import lookup_mfe

    hist_mfe, hist_n = lookup_mfe(strategy_version=None, horizon_min=60)
    target_plan = build_target_plan(
        entry=entry,
        stop=stop,
        facts=facts,
        min_rr=min_rr,
        historical_mfe_pct=hist_mfe,
        historical_sample_size=hist_n,
    )
    # Recompute facts with adaptive target for remaining reward.
    facts = evaluate_timing(
        exec_snap,
        signal_price=float(close),
        planned_entry=float(entry),
        planned_stop=float(stop),
        planned_target=float(target_plan.price),
        market=market,
    )
    bundle = decide_entry(
        InstrumentThesis.BULLISH,
        facts,
        market=market,
        technical_score=technical.score,
        news_score=news.score,
        target=target_plan,
        stop_price=float(stop),
    )

    setup_type = SetupType.PULLBACK_CONTINUATION

    target = target_plan.price
    rr = float((target - entry) / risk)
    if rr < 1.0 or target <= entry:
        return None, None

    confidence = min(0.95, overall / 100.0)
    reasons = [
        f"Setup confluence ({STRATEGY_VERSION})",
        f"Technical {technical.score}/100 ({technical.trend})",
        f"News {news.sentiment} ({news.score}/100)",
        f"Market {market.regime.value} / {market.risk_posture}",
        f"Overall {overall}/100",
        (
            f"Thesis BULLISH · entry {bundle.entry_decision.value} · "
            f"setup {bundle.setup_quality}/100 · entry {bundle.entry_quality}/100"
        ),
        f"Target model {target_plan.model} · {target_plan.reachability.value}",
        *conf_reasons[:4],
        *bundle.chase_reasons[:3],
        *technical.reasons[:2],
    ]

    candidate = TradeCandidate(
        symbol=symbol.upper(),
        action=TradeAction.BUY,
        confidence=confidence,
        entry=entry,
        stop=stop,
        target=target,
        risk_reward=round(rr, 2),
        reasons=reasons,
        strategy_version=STRATEGY_VERSION,
        technical_score=technical.score,
        quant_score=quant_score,
        news_label=news.sentiment,
        market_label=market.regime.value,
        pipeline_run_id=pipeline_run_id,
        exec_timeframe=exec_snap.timeframe,
        thesis=InstrumentThesis.BULLISH,
        entry_decision=bundle.entry_decision,
        entry_quality=bundle.entry_quality,
        setup_type=setup_type,
        setup_quality=bundle.setup_quality,
        setup_quality_breakdown=bundle.setup_breakdown.as_dict() if bundle.setup_breakdown else {},
        entry_quality_breakdown=bundle.breakdown.as_dict(),
        chase_reasons=list(bundle.chase_reasons),
        signal_price=signal_price,
        entry_zone_low=bundle.entry_zone_low,
        entry_zone_high=bundle.entry_zone_high,
        target_model=target_plan.model,
        target_reachability=target_plan.reachability,
        session_cohort=facts.session_cohort,
    )
    return candidate, bundle
