"""Universe agent — liquidity/price + Stage 8 multi-TF feature load.

Fetches 1D (required), 1H (preferred), aggregates 4H from 1H, and 15m when
available. Same path for paper and live market-data ports.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from agents.trader.types import StepResult, TraderBundle, TraderStep
from core.enums import Timeframe
from core.ports import MarketDataPort
from core.vendor_http import describe_http_error
from quant.aggregate import aggregate_bars
from quant.engine import compute_features
from trading.gates import check_bar_freshness

PROMPT_VERSION = "trader.universe@1.2.0"

MIN_PRICE = Decimal(5)
MAX_PRICE = Decimal(2000)
MIN_ADV_USD = 20_000_000.0
MIN_BARS = 60
MIN_H1_BARS = 40
MIN_M15_BARS = 40


def _stale_result(bundle: TraderBundle, *, timeframe: Timeframe, newest: object) -> StepResult:
    """Refuse the desk when a series used for structure/entry has stopped."""
    result = StepResult(
        step=TraderStep.UNIVERSE,
        ok=False,
        detail=f"Stale {timeframe.value} bars",
        reasons=[
            "STALE_BARS",
            f"{timeframe.value}:newest={newest}",
        ],
        score=0,
    )
    bundle.record(result)
    return result


async def run_universe(bundle: TraderBundle, md: MarketDataPort) -> StepResult:
    symbol = bundle.symbol
    end = datetime.now(UTC)
    start = end - timedelta(days=400)
    reasons: list[str] = []

    try:
        bars = await md.get_bars(symbol, Timeframe.D1, start, end)
    except Exception as exc:  # noqa: BLE001
        result = StepResult(
            step=TraderStep.UNIVERSE,
            ok=False,
            detail="Alpaca bars failed",
            reasons=["UNIVERSE_ALPACA_ERROR", describe_http_error(exc)],
            score=0,
        )
        bundle.record(result)
        return result

    if len(bars) < MIN_BARS:
        result = StepResult(
            step=TraderStep.UNIVERSE,
            ok=False,
            detail="Too few daily bars",
            reasons=["UNIVERSE_THIN_HISTORY"],
            score=0,
        )
        bundle.record(result)
        return result

    d1_fresh = check_bar_freshness(symbol, bars, now=end)
    if not d1_fresh.passed:
        return _stale_result(
            bundle, timeframe=Timeframe.D1, newest=d1_fresh.measured.get("newest_bar")
        )

    d1 = compute_features(symbol, Timeframe.D1, bars)
    bundle.features[Timeframe.D1] = d1

    close = d1.indicators.get("close")
    adv = d1.indicators.get("avg_dollar_volume")
    price = Decimal(str(close)) if isinstance(close, (int, float)) else None

    if price is None or price < MIN_PRICE or price > MAX_PRICE:
        reasons.append(f"price_out_of_band={price}")
        result = StepResult(
            step=TraderStep.UNIVERSE,
            ok=False,
            detail="Price filter failed",
            reasons=["UNIVERSE_PRICE", *reasons],
            score=0,
        )
        bundle.record(result)
        return result

    bundle.last_price = price
    adv_f = float(adv) if isinstance(adv, (int, float)) else 0.0
    if adv_f < MIN_ADV_USD:
        result = StepResult(
            step=TraderStep.UNIVERSE,
            ok=False,
            detail="ADV too low",
            reasons=["UNIVERSE_ADV", f"adv_usd={adv_f:.0f}"],
            score=10,
        )
        bundle.record(result)
        return result

    # H1 preferred — drives setup/entry. When present must be fresh.
    h1_bars = None
    try:
        h1_bars = await md.get_bars(symbol, Timeframe.H1, end - timedelta(days=60), end)
        if len(h1_bars) >= MIN_H1_BARS:
            h1_fresh = check_bar_freshness(symbol, h1_bars, now=end)
            if not h1_fresh.passed:
                return _stale_result(
                    bundle,
                    timeframe=Timeframe.H1,
                    newest=h1_fresh.measured.get("newest_bar"),
                )
            bundle.features[Timeframe.H1] = compute_features(symbol, Timeframe.H1, h1_bars)
            reasons.append("tf=H1")
            # Stage 8: 4H from H1 aggregation (same series, no extra vendor call).
            h4_bars = aggregate_bars(h1_bars, Timeframe.H4, source_label="agg:1h")
            if len(h4_bars) >= 30:
                bundle.features[Timeframe.H4] = compute_features(symbol, Timeframe.H4, h4_bars)
                reasons.append("tf=H4")
    except Exception:  # noqa: BLE001, S110 — H1 optional when vendor thin
        pass

    # 15m — entry timing context when available.
    try:
        m15_bars = await md.get_bars(symbol, Timeframe.M15, end - timedelta(days=14), end)
        if len(m15_bars) >= MIN_M15_BARS:
            m15_fresh = check_bar_freshness(symbol, m15_bars, now=end)
            if m15_fresh.passed:
                bundle.features[Timeframe.M15] = compute_features(symbol, Timeframe.M15, m15_bars)
                reasons.append("tf=M15")
    except Exception:  # noqa: BLE001, S110
        pass

    score = 70 if adv_f >= MIN_ADV_USD * 2 else 55
    if Timeframe.H4 in bundle.features:
        score = min(100, score + 5)
    if Timeframe.M15 in bundle.features:
        score = min(100, score + 5)
    reasons = [f"price={price}", f"adv_usd={adv_f:.0f}", *reasons]
    result = StepResult(
        step=TraderStep.UNIVERSE,
        ok=True,
        detail="liquid US equity",
        reasons=reasons,
        score=score,
    )
    bundle.record(result)
    return result
