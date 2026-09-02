"""Market / regime Agent — FRED when available, else neutral stub."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from core.enums import AssessmentKind, MarketRegimeLabel
from core.schemas import MarketAssessment

PROMPT_VERSION = "market@0.1.0"


async def assess_market(
    fred_api_key: str | None = None,
    *,
    now: datetime | None = None,
) -> MarketAssessment:
    evaluated_at = now or datetime.now(UTC)
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    else:
        evaluated_at = evaluated_at.astimezone(UTC)

    if not fred_api_key:
        # Fail closed: missing FRED is not a tradable NEUTRAL. Capital-path
        # market_gate rejects assessments without a trustworthy timestamp.
        return MarketAssessment(
            kind=AssessmentKind.MARKET,
            regime=MarketRegimeLabel.NEUTRAL,
            score=0,
            risk_posture="unknown",
            reasons=["FRED_NOT_CONFIGURED", "DATA_BLOCKED"],
            macro_notes=[],
            evaluated_at=None,
            benchmark=None,
            sector_label=None,
            sector_tradable=None,
        )

    # 10Y yield (DGS10) and unemployment (UNRATE) — simple regime proxy
    async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
        dgs = await _fred_latest(client, fred_api_key, "DGS10")
        unrate = await _fred_latest(client, fred_api_key, "UNRATE")

    notes: list[str] = []
    score = 55
    regime = MarketRegimeLabel.NEUTRAL
    posture = "neutral"

    if dgs is not None:
        notes.append(f"DGS10={dgs:.2f}")
        if dgs >= 4.5:
            score -= 10
            posture = "risk_off"
            regime = MarketRegimeLabel.RISK_OFF
            notes.append("Elevated yields — caution")
        elif dgs <= 3.0:
            score += 8
            posture = "risk_on"
            regime = MarketRegimeLabel.RISK_ON

    if unrate is not None:
        notes.append(f"UNRATE={unrate:.2f}")
        if unrate >= 5.0:
            score -= 8
            posture = "risk_off"
            regime = MarketRegimeLabel.RISK_OFF

    score = max(0, min(100, score))
    return MarketAssessment(
        kind=AssessmentKind.MARKET,
        regime=regime,
        score=score,
        risk_posture=posture,
        reasons=["FRED macro snapshot"] + notes,
        macro_notes=notes,
        evaluated_at=evaluated_at,
        benchmark="FRED:DGS10+UNRATE",
        sector_label="unknown",
        sector_tradable=True,
    )


async def _fred_latest(client: httpx.AsyncClient, key: str, series_id: str) -> float | None:
    url = "https://api.stlouisfed.org/fred/series/observations"
    params: dict[str, str | int] = {
        "series_id": series_id,
        "api_key": key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 5,
    }
    resp = await client.get(url, params=params)
    resp.raise_for_status()
    obs = (resp.json() or {}).get("observations") or []
    for row in obs:
        val = row.get("value")
        if val not in (None, "."):
            return float(val)
    return None
