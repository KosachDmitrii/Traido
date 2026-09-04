"""Market / regime Agent — FRED when available, else fail-closed stub."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from core.enums import AssessmentKind, MarketRegimeLabel
from core.schemas import MarketAssessment

PROMPT_VERSION = "market@0.1.0"


def _blocked(
    *,
    reasons: list[str],
    notes: list[str] | None = None,
) -> MarketAssessment:
    """Untrusted / incomplete FRED — never tradable, never stamped."""
    return MarketAssessment(
        kind=AssessmentKind.MARKET,
        regime=MarketRegimeLabel.NEUTRAL,
        score=0,
        risk_posture="unknown",
        reasons=reasons,
        macro_notes=list(notes or []),
        evaluated_at=None,
        benchmark=None,
        sector_label=None,
        sector_tradable=None,
    )


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
        return _blocked(reasons=["FRED_NOT_CONFIGURED", "DATA_BLOCKED"])

    async with httpx.AsyncClient(timeout=20.0, trust_env=False) as client:
        dgs = await _fred_latest(client, fred_api_key, "DGS10")
        unrate = await _fred_latest(client, fred_api_key, "UNRATE")

    if dgs is None or unrate is None:
        missing = []
        if dgs is None:
            missing.append("DGS10")
        if unrate is None:
            missing.append("UNRATE")
        return _blocked(
            reasons=["FRED_SERIES_EMPTY", "DATA_BLOCKED", *[f"MISSING_{s}" for s in missing]],
        )

    from risk.limits import load_macro_regime

    th = load_macro_regime()
    notes: list[str] = [f"DGS10={dgs:.2f}", f"UNRATE={unrate:.2f}"]
    score = 55
    regime = MarketRegimeLabel.NEUTRAL
    posture = "neutral"

    if dgs >= th.dgs10_risk_off:
        score -= 10
        posture = "risk_off"
        regime = MarketRegimeLabel.RISK_OFF
        notes.append("Elevated yields — caution")
    elif dgs <= th.dgs10_risk_on:
        score += 8
        posture = "risk_on"
        regime = MarketRegimeLabel.RISK_ON

    if unrate >= th.unrate_risk_off:
        score -= 8
        posture = "risk_off"
        regime = MarketRegimeLabel.RISK_OFF

    score = max(0, min(100, score))
    # FRED series alone are not a sector assessment. Never invent
    # sector_label="unknown" with sector_tradable=True.
    return MarketAssessment(
        kind=AssessmentKind.MARKET,
        regime=regime,
        score=score,
        risk_posture=posture,
        reasons=["FRED macro snapshot", "SECTOR_ASSESSMENT_REQUIRED"] + notes,
        macro_notes=notes,
        evaluated_at=evaluated_at,
        benchmark="FRED:DGS10+UNRATE",
        sector_label=None,
        sector_tradable=None,
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
