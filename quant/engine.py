"""Quant feature engine — builds FeatureSnapshot from OHLCV bars."""

from __future__ import annotations

from datetime import UTC, datetime

from core.enums import Timeframe
from core.schemas import Bar, FeatureSnapshot
from quant.candlestick_patterns import detect_candles
from quant.chart_patterns import detect_chart_patterns
from quant.indicators import (
    atr,
    bollinger,
    ema,
    last,
    macd,
    relative_volume,
    rsi,
    sma,
    vwap,
)
from quant.market_regime import classify
from quant.momentum import compute_momentum
from quant.series import closes, highs, lows, opens, volumes
from quant.support_resistance import support_resistance
from quant.volatility import average_dollar_volume, compute_volatility, volume_trend


def compute_features(symbol: str, timeframe: Timeframe, bars: list[Bar]) -> FeatureSnapshot:
    if not bars:
        raise ValueError(f"no bars for {symbol} {timeframe}")

    c = closes(bars)
    h = highs(bars)
    l = lows(bars)
    o = opens(bars)
    v = volumes(bars)

    rsi_s = rsi(c, 14)
    ema50 = ema(c, 50)
    ema200 = ema(c, 200)
    sma20 = sma(c, 20)
    sma50 = sma(c, 50)
    macd_line, macd_signal, macd_hist = macd(c)
    atr_s = atr(h, l, c, 14)
    bb_u, bb_m, bb_l = bollinger(c, 20, 2.0)
    vwap_s = vwap(h, l, c, v)
    rvol = relative_volume(v, 20)

    momentum = compute_momentum(bars)
    volatility = compute_volatility(bars)
    regime = classify(bars)

    e50 = last(ema50)
    e200 = last(ema200)
    indicators: dict[str, float | int | bool | str | None] = {
        "close": c[-1],
        "rsi_14": last(rsi_s),
        "ema_50": e50,
        "ema_200": e200,
        "sma_20": last(sma20),
        "sma_50": last(sma50),
        "macd": last(macd_line),
        "macd_signal": last(macd_signal),
        "macd_hist": last(macd_hist),
        "atr_14": last(atr_s),
        "bb_upper": last(bb_u),
        "bb_mid": last(bb_m),
        "bb_lower": last(bb_l),
        "vwap": last(vwap_s),
        "relative_volume": rvol,
        "ema50_above_ema200": None if e50 is None or e200 is None else e50 > e200,
        # Momentum
        "roc_21": momentum.roc.get(21),
        "roc_63": momentum.roc.get(63),
        "roc_126": momentum.roc.get(126),
        "roc_252": momentum.roc.get(252),
        "momentum_12_1": momentum.momentum_12_1,
        "momentum_risk_adjusted": momentum.risk_adjusted,
        "momentum_score": momentum.score(),
        "momentum_accelerating": momentum.accelerating,
        # Volatility
        "atr_pct": volatility.atr_pct,
        "realised_vol_annual_pct": volatility.realised_vol_annual_pct,
        "parkinson_vol_annual_pct": volatility.parkinson_vol_annual_pct,
        "volatility_percentile": volatility.vol_percentile,
        "bollinger_bandwidth_pct": volatility.bollinger_bandwidth_pct,
        "volatility_squeeze": volatility.squeeze,
        "volatility_expansion": volatility.expansion,
        # Liquidity
        "avg_dollar_volume": average_dollar_volume(bars, 20),
        "volume_trend": volume_trend(bars),
        # Regime
        "regime": regime.label.value,
        "regime_trend_strength_pct": regime.trend_strength_pct,
        "regime_tradable_long": regime.is_tradable_long,
    }

    candles = detect_candles(o, h, l, c)
    charts = detect_chart_patterns(h, l, c)
    support, resistance = support_resistance(h, l)

    notes: list[str] = []
    if indicators["ema50_above_ema200"] is True:
        notes.append("EMA50 above EMA200")
    if rvol is not None and rvol >= 1.5:
        notes.append(f"Elevated relative volume {rvol:.2f}")
    rsi_v = indicators["rsi_14"]
    if isinstance(rsi_v, float):
        if rsi_v >= 70:
            notes.append("RSI overbought zone")
        elif rsi_v <= 30:
            notes.append("RSI oversold zone")
    notes.extend(momentum.reasons)
    notes.extend(volatility.reasons)
    notes.extend(regime.reasons)

    return FeatureSnapshot(
        symbol=symbol.upper(),
        timeframe=timeframe,
        computed_at=datetime.now(UTC),
        indicators=indicators,
        candlestick_patterns=candles,
        chart_patterns=charts,
        support=support,
        resistance=resistance,
        notes=notes,
    )


def compute_multi_timeframe(
    symbol: str,
    bars_by_tf: dict[Timeframe, list[Bar]],
) -> dict[Timeframe, FeatureSnapshot]:
    return {tf: compute_features(symbol, tf, bars) for tf, bars in bars_by_tf.items() if bars}
