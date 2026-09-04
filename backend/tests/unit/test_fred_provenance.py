"""FRED observations keep their print date; stale prints are DATA_BLOCKED."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from agents.market.agent import FredObservation, assess_market


@pytest.mark.asyncio
async def test_stale_dgs10_is_data_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched = datetime(2026, 9, 4, 15, 0, tzinfo=UTC)
    stale = fetched.date() - timedelta(days=20)

    async def _latest(_client, _key, series, *, fetched_at=None):
        value = {"DGS10": 4.20, "UNRATE": 4.10}[series]
        obs = stale if series == "DGS10" else fetched.date()
        return FredObservation(series, value, obs, fetched_at or fetched)

    monkeypatch.setattr("agents.market.agent._fred_latest", _latest)
    result = await assess_market("fake-key", now=fetched)
    assert result.evaluated_at is None
    assert "FRED_OBSERVATION_STALE" in result.reasons
    assert "DATA_BLOCKED" in result.reasons


@pytest.mark.asyncio
async def test_fresh_observation_keeps_print_date(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched = datetime(2026, 9, 4, 15, 0, tzinfo=UTC)
    dgs_day = date(2026, 9, 3)
    unrate_day = date(2026, 8, 1)

    async def _latest(_client, _key, series, *, fetched_at=None):
        if series == "DGS10":
            return FredObservation("DGS10", 4.20, dgs_day, fetched_at or fetched)
        return FredObservation("UNRATE", 4.10, unrate_day, fetched_at or fetched)

    monkeypatch.setattr("agents.market.agent._fred_latest", _latest)
    result = await assess_market("fake-key", now=fetched)
    assert result.observation_date == dgs_day
    assert result.fetched_at == fetched
    assert result.evaluated_at == fetched
    assert any("DGS10_OBS=2026-09-03" in n for n in result.macro_notes)
