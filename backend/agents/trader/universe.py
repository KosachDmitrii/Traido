"""Universe agent — is this symbol worth analysing (Alpaca liquidity/price)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from agents.trader.types import StepResult, TraderBundle, TraderStep
from core.enums import Timeframe
from core.ports import MarketDataPort
from quant.engine import compute_features

PROMPT_VERSION = "trader.universe@1.0.0"

MIN_PRICE = Decimal(5)
MAX_PRICE = Decimal(2000)
MIN_ADV_USD = 20_000_000.0
MIN_BARS = 60


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
            reasons=["UNIVERSE_ALPACA_ERROR", str(exc)[:120]],
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

    # Optional H1 for later steps — fail soft if missing (illiquid hours).
    try:
        h1_bars = await md.get_bars(symbol, Timeframe.H1, end - timedelta(days=60), end)
        if len(h1_bars) >= 40:
            bundle.features[Timeframe.H1] = compute_features(symbol, Timeframe.H1, h1_bars)
    except Exception:  # noqa: BLE001, S110 — H1 is optional enrichment
        pass

    score = 70 if adv_f >= MIN_ADV_USD * 2 else 55
    reasons = [f"price={price}", f"adv_usd={adv_f:.0f}"]
    result = StepResult(
        step=TraderStep.UNIVERSE,
        ok=True,
        detail="liquid US equity",
        reasons=reasons,
        score=score,
    )
    bundle.record(result)
    return result
