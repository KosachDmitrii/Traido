"""News Agent — Finnhub when available, otherwise neutral stub."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from core.enums import AssessmentKind, NewsCheck
from core.schemas import NewsAssessment
from core.vendor_http import describe_http_error, get_with_retry

PROMPT_VERSION = "news@0.2.0"


def _unread(symbol: str, status: NewsCheck, why: str) -> NewsAssessment:
    """An assessment that says it is not one.

    The scores are neutral because something has to go in the fields, but
    `status` is what the risk engine reads — nothing downstream may treat these
    fifty points as evidence that the headlines were clean.
    """
    return NewsAssessment(
        kind=AssessmentKind.NEWS,
        symbol=symbol,
        sentiment="neutral",
        score=50,
        material_events=[],
        headlines=[],
        reasons=[why],
        status=status,
    )


async def assess_news(
    symbol: str,
    finnhub_api_key: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> NewsAssessment:
    symbol = symbol.upper()
    if not finnhub_api_key:
        return _unread(symbol, NewsCheck.NOT_CONFIGURED, "Finnhub key not configured")

    end = datetime.now(UTC).date()
    start = end - timedelta(days=7)
    url = "https://finnhub.io/api/v1/company-news"
    params = {
        "symbol": symbol,
        "from": start.isoformat(),
        "to": end.isoformat(),
    }
    # Header rather than the `token=` query parameter Finnhub also accepts: a
    # key in the URL ends up in `HTTPStatusError`, and from there in the desk
    # log and the audit trail. Finnhub supports both on every endpoint.
    headers = {"X-Finnhub-Token": finnhub_api_key}
    try:
        async with httpx.AsyncClient(timeout=20.0, trust_env=False, transport=transport) as client:
            resp = await get_with_retry(client, url, params=params, headers=headers)
            items = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        # Reported rather than raised. Raising took the whole symbol's pipeline
        # down, and the scanner recorded that as `no_candidate` — the same
        # bucket as "there was no setup here", which is the one thing the
        # operator most needs it not to be confused with. Named by status code
        # rather than by the vendor's own message, which carries the request
        # URL. See `core.vendor_http.describe_http_error`.
        return _unread(
            symbol, NewsCheck.UNAVAILABLE, f"News lookup failed: {describe_http_error(exc)}"
        )

    if not isinstance(items, list):
        return _unread(symbol, NewsCheck.UNAVAILABLE, "News response was not a list of articles")

    headlines = [str(i.get("headline") or "")[:180] for i in items[:8] if i.get("headline")]
    text = " ".join(headlines).lower()
    pos_words = ("beat", "surge", "record", "upgrade", "growth", "profit", "raises")
    neg_words = ("miss", "cut", "downgrade", "probe", "lawsuit", "fraud", "slump", "recall")
    pos = sum(1 for w in pos_words if w in text)
    neg = sum(1 for w in neg_words if w in text)

    if pos > neg + 1:
        sentiment, score = "positive", min(90, 55 + pos * 8)
    elif neg > pos + 1:
        sentiment, score = "negative", max(10, 45 - neg * 8)
    elif pos or neg:
        sentiment, score = "mixed", 50
    else:
        sentiment, score = "neutral", 50

    return NewsAssessment(
        kind=AssessmentKind.NEWS,
        symbol=symbol,
        sentiment=sentiment,
        score=score,
        material_events=[],
        headlines=headlines,
        reasons=[
            f"Headline sentiment heuristic ({sentiment})",
            f"Articles considered: {len(headlines)}",
        ],
        status=NewsCheck.CHECKED,
    )
