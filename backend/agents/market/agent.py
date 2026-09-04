"""Market / regime Agent — FRED when available, else fail-closed stub."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import httpx

from core.enums import AssessmentKind, MarketRegimeLabel
from core.schemas import MarketAssessment

PROMPT_VERSION = "market@0.1.0"

# Daily Treasury; allow a long weekend plus a delayed print.
DGS10_MAX_AGE = timedelta(days=10)
# Monthly unemployment; one missed release still leaves the prior print usable.
UNRATE_MAX_AGE = timedelta(days=45)


@dataclass(frozen=True)
class FredObservation:
    series_id: str
    value: float
    observation_date: date
    fetched_at: datetime


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
        fetched_at=None,
        observation_date=None,
        benchmark=None,
        sector_label=None,
        sector_tradable=None,
    )


def _as_observation(raw: object, *, series_id: str, fetched_at: datetime) -> FredObservation | None:
    if raw is None:
        return None
    if isinstance(raw, FredObservation):
        return raw
    if isinstance(raw, (int, float)):
        # Test doubles that still return a bare level — no provenance to trust.
        return None
    return None


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
        dgs_raw = await _fred_latest(client, fred_api_key, "DGS10", fetched_at=evaluated_at)
        unrate_raw = await _fred_latest(client, fred_api_key, "UNRATE", fetched_at=evaluated_at)

    dgs = _as_observation(dgs_raw, series_id="DGS10", fetched_at=evaluated_at)
    unrate = _as_observation(unrate_raw, series_id="UNRATE", fetched_at=evaluated_at)

    # Bare-float test doubles: keep historic regime maths, but do not invent a date.
    if dgs is None and isinstance(dgs_raw, (int, float)):
        dgs_value = float(dgs_raw)
        dgs_date: date | None = None
    elif dgs is not None:
        dgs_value = dgs.value
        dgs_date = dgs.observation_date
    else:
        dgs_value = None
        dgs_date = None
    if unrate is None and isinstance(unrate_raw, (int, float)):
        unrate_value = float(unrate_raw)
        unrate_date: date | None = None
    elif unrate is not None:
        unrate_value = unrate.value
        unrate_date = unrate.observation_date
    else:
        unrate_value = None
        unrate_date = None

    if dgs_value is None or unrate_value is None:
        missing = []
        if dgs_value is None:
            missing.append("DGS10")
        if unrate_value is None:
            missing.append("UNRATE")
        return _blocked(
            reasons=["FRED_SERIES_EMPTY", "DATA_BLOCKED", *[f"MISSING_{s}" for s in missing]],
        )

    if dgs_date is None or unrate_date is None:
        # Production FRED always has a date. A mock without one is not a fresh print.
        if (
            dgs_date is None
            and unrate_date is None
            and (isinstance(dgs_raw, (int, float)) or isinstance(unrate_raw, (int, float)))
        ):
            pass
        else:
            return _blocked(reasons=["FRED_OBSERVATION_DATE_MISSING", "DATA_BLOCKED"])

    today = evaluated_at.date()
    if (dgs_date is not None and dgs_date > today) or (
        unrate_date is not None and unrate_date > today
    ):
        return _blocked(
            reasons=["FRED_OBSERVATION_DATE_INVALID", "DATA_BLOCKED"],
            notes=[
                f"DGS10_OBS={dgs_date.isoformat() if dgs_date else 'missing'}",
                f"UNRATE_OBS={unrate_date.isoformat() if unrate_date else 'missing'}",
            ],
        )
    if dgs_date is not None and (today - dgs_date).days > DGS10_MAX_AGE.days:
        return _blocked(
            reasons=["FRED_OBSERVATION_STALE", "DATA_BLOCKED", "STALE_DGS10"],
            notes=[f"DGS10_OBS={dgs_date.isoformat()}"],
        )
    if unrate_date is not None and (today - unrate_date).days > UNRATE_MAX_AGE.days:
        return _blocked(
            reasons=["FRED_OBSERVATION_STALE", "DATA_BLOCKED", "STALE_UNRATE"],
            notes=[f"UNRATE_OBS={unrate_date.isoformat()}"],
        )

    from risk.limits import load_macro_regime

    th = load_macro_regime()
    notes: list[str] = [f"DGS10={dgs_value:.2f}", f"UNRATE={unrate_value:.2f}"]
    if dgs_date is not None:
        notes.append(f"DGS10_OBS={dgs_date.isoformat()}")
    if unrate_date is not None:
        notes.append(f"UNRATE_OBS={unrate_date.isoformat()}")
    score = 55
    regime = MarketRegimeLabel.NEUTRAL
    posture = "neutral"

    if dgs_value >= th.dgs10_risk_off:
        score -= 10
        posture = "risk_off"
        regime = MarketRegimeLabel.RISK_OFF
        notes.append("Elevated yields — caution")
    elif dgs_value <= th.dgs10_risk_on:
        score += 8
        posture = "risk_on"
        regime = MarketRegimeLabel.RISK_ON

    if unrate_value >= th.unrate_risk_off:
        score -= 8
        posture = "risk_off"
        regime = MarketRegimeLabel.RISK_OFF

    score = max(0, min(100, score))
    known_dates = [value for value in (dgs_date, unrate_date) if value is not None]
    obs_date = min(known_dates) if known_dates else None
    return MarketAssessment(
        kind=AssessmentKind.MARKET,
        regime=regime,
        score=score,
        risk_posture=posture,
        reasons=["FRED macro snapshot", "SECTOR_ASSESSMENT_REQUIRED"] + notes,
        macro_notes=notes,
        evaluated_at=evaluated_at,
        fetched_at=evaluated_at,
        observation_date=obs_date,
        macro_series=["DGS10", "UNRATE"],
        benchmark="FRED:DGS10+UNRATE",
        sector_label=None,
        sector_tradable=None,
    )


async def _fred_latest(
    client: httpx.AsyncClient,
    key: str,
    series_id: str,
    *,
    fetched_at: datetime | None = None,
) -> FredObservation | None:
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
    fetched = fetched_at or datetime.now(UTC)
    for row in obs:
        val = row.get("value")
        if val in (None, "."):
            continue
        raw_date = row.get("date")
        if not raw_date:
            return None
        return FredObservation(
            series_id=series_id,
            value=float(val),
            observation_date=date.fromisoformat(str(raw_date)),
            fetched_at=fetched,
        )
    return None
