"""Checklist agent — final pass before a buy proposal (Alpaca quote + news)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from agents.trader.types import StepResult, TraderBundle, TraderStep
from core.config import Settings, get_settings
from core.enums import AssessmentKind, NewsCheck
from core.ports import MarketDataPort
from core.schemas import NewsAssessment
from core.vendor_http import describe_http_error, get_with_retry

PROMPT_VERSION = "trader.checklist@1.0.0"
MAX_SPREAD_BPS = 40.0
NEG_WORDS = ("downgrade", "probe", "lawsuit", "fraud", "recall", "bankrupt", "investigation")


async def _alpaca_news(symbol: str, settings: Settings) -> NewsAssessment:
    key = settings.alpaca_api_key
    secret = settings.alpaca_api_secret
    if not key or not secret:
        return NewsAssessment(
            kind=AssessmentKind.NEWS,
            symbol=symbol,
            sentiment="neutral",
            score=0,
            reasons=["ALPACA_KEYS_MISSING"],
            status=NewsCheck.NOT_CONFIGURED,
        )

    headers = {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
    }
    url = f"{settings.alpaca_data_base_url.rstrip('/')}/v1beta1/news"
    params: dict[str, Any] = {
        "symbols": symbol,
        "limit": 10,
        "include_content": "false",
        "exclude_contentless": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
            resp = await get_with_retry(client, url, params=params, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        return NewsAssessment(
            kind=AssessmentKind.NEWS,
            symbol=symbol,
            sentiment="neutral",
            score=0,
            reasons=["ALPACA_NEWS_ERROR", describe_http_error(exc)],
            status=NewsCheck.UNAVAILABLE,
        )

    articles = payload.get("news") if isinstance(payload, dict) else None
    if not isinstance(articles, list):
        return NewsAssessment(
            kind=AssessmentKind.NEWS,
            symbol=symbol,
            sentiment="neutral",
            score=0,
            reasons=["ALPACA_NEWS_BAD_SHAPE"],
            status=NewsCheck.UNAVAILABLE,
        )

    headlines: list[str] = []
    neg = 0
    for row in articles[:10]:
        if not isinstance(row, dict):
            continue
        title = str(row.get("headline") or row.get("title") or "").strip()
        if not title:
            continue
        headlines.append(title[:160])
        low = title.lower()
        if any(w in low for w in NEG_WORDS):
            neg += 1

    if neg >= 2:
        sentiment, score = "negative", 25
    elif neg == 1:
        sentiment, score = "mixed", 45
    elif headlines:
        sentiment, score = "neutral", 55
    else:
        sentiment, score = "neutral", 50

    return NewsAssessment(
        kind=AssessmentKind.NEWS,
        symbol=symbol,
        sentiment=sentiment,
        score=score,
        headlines=headlines[:5],
        reasons=[f"alpaca_news_n={len(headlines)}", f"neg_hits={neg}"],
        status=NewsCheck.CHECKED,
    )


async def run_checklist(
    bundle: TraderBundle,
    md: MarketDataPort,
    *,
    settings: Settings | None = None,
) -> StepResult:
    settings = settings or get_settings()
    reasons: list[str] = []

    # Live quote / spread from Alpaca
    quote = None
    get_quote = getattr(md, "get_quote", None)
    if get_quote is not None:
        try:
            quote = await get_quote(bundle.symbol)
        except Exception as exc:  # noqa: BLE001
            result = StepResult(
                step=TraderStep.CHECKLIST,
                ok=False,
                detail="Quote failed",
                reasons=["CHECKLIST_QUOTE_ERROR", describe_http_error(exc)],
                score=0,
            )
            bundle.record(result)
            return result

    if quote is None or quote.bid is None or quote.ask is None:
        result = StepResult(
            step=TraderStep.CHECKLIST,
            ok=False,
            detail="No live quote",
            reasons=["CHECKLIST_NO_QUOTE"],
            score=0,
        )
        bundle.record(result)
        return result

    mid = (quote.bid + quote.ask) / 2
    if mid <= 0:
        result = StepResult(
            step=TraderStep.CHECKLIST,
            ok=False,
            detail="Bad mid",
            reasons=["CHECKLIST_BAD_MID"],
            score=0,
        )
        bundle.record(result)
        return result

    spread_bps = float((quote.ask - quote.bid) / mid * Decimal(10000))
    bundle.quote_spread_bps = spread_bps
    reasons.append(f"spread_bps={spread_bps:.1f}")
    if spread_bps > MAX_SPREAD_BPS:
        result = StepResult(
            step=TraderStep.CHECKLIST,
            ok=False,
            detail="Spread too wide",
            reasons=[*reasons, "CHECKLIST_SPREAD"],
            score=20,
        )
        bundle.record(result)
        return result

    if quote.ts is not None:
        age = datetime.now(UTC) - (quote.ts if quote.ts.tzinfo else quote.ts.replace(tzinfo=UTC))
        if age > timedelta(minutes=5):
            result = StepResult(
                step=TraderStep.CHECKLIST,
                ok=False,
                detail="Quote stale",
                reasons=[*reasons, "CHECKLIST_QUOTE_STALE", f"age_s={age.total_seconds():.0f}"],
                score=15,
            )
            bundle.record(result)
            return result

    news = await _alpaca_news(bundle.symbol, settings)
    bundle.news = news
    if news.status is not NewsCheck.CHECKED:
        result = StepResult(
            step=TraderStep.CHECKLIST,
            ok=False,
            detail="News unverified",
            reasons=[*reasons, "CHECKLIST_NEWS", news.status.value, *news.reasons[:2]],
            score=0,
        )
        bundle.record(result)
        return result
    if news.sentiment == "negative":
        result = StepResult(
            step=TraderStep.CHECKLIST,
            ok=False,
            detail="Negative news",
            reasons=[*reasons, "CHECKLIST_NEWS_NEG", *news.headlines[:2]],
            score=news.score,
        )
        bundle.record(result)
        return result

    reasons.append(f"news={news.sentiment}")
    result = StepResult(
        step=TraderStep.CHECKLIST,
        ok=True,
        detail="ready to propose",
        reasons=reasons,
        score=85,
    )
    bundle.record(result)
    return result
