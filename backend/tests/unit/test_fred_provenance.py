"""FRED observations keep their print date; stale prints are DATA_BLOCKED."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from agents.market.agent import FredObservation, assess_market
from trading.market_gate import evaluate_market_gate


@pytest.mark.asyncio
async def test_fred_http_failure_blocks_with_safe_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _latest(_client, key, _series, *, fetched_at=None):
        request = httpx.Request("GET", f"https://example.org/?api_key={key}")
        response = httpx.Response(502, request=request)
        raise httpx.HTTPStatusError("failed", request=request, response=response)

    monkeypatch.setattr("agents.market.agent._fred_latest", _latest)
    result = await assess_market("test-secret-value-12345")
    gate = evaluate_market_gate(result, require_sector=False)
    assert result.evaluated_at is None
    assert "FRED_HTTP_502" in gate.reason_codes
    assert "test-secret-value-12345" not in repr(gate)


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
    gate = evaluate_market_gate(result, now=fetched, require_sector=False)
    assert gate.tradable_long is False
    assert "REGIME_TIMESTAMP_MISSING" in gate.reason_codes
    assert "FRED_OBSERVATION_STALE" in gate.reason_codes


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
    assert result.observation_date == unrate_day
    assert result.fetched_at == fetched
    assert result.evaluated_at == fetched
    assert any("DGS10_OBS=2026-09-03" in n for n in result.macro_notes)


@pytest.mark.parametrize("day", [date(2026, 9, 29), date(2026, 10, 1)])
@pytest.mark.asyncio
async def test_august_unrate_is_current_across_month_boundary(
    monkeypatch: pytest.MonkeyPatch, day: date
) -> None:
    evaluated = datetime(day.year, day.month, day.day, 15, 0, tzinfo=UTC)

    async def _latest(_client, _key, series, *, fetched_at=None):
        if series == "DGS10":
            return FredObservation(series, 4.20, day - timedelta(days=1), evaluated)
        return FredObservation(series, 4.10, date(2026, 8, 1), evaluated)

    monkeypatch.setattr("agents.market.agent._fred_latest", _latest)
    result = await assess_market("fake-key", now=evaluated)
    assert result.evaluated_at == evaluated
    assert result.observation_date == date(2026, 8, 1)
    assert "FRED_OBSERVATION_STALE" not in result.reasons


@pytest.mark.asyncio
async def test_unrate_three_calendar_months_old_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evaluated = datetime(2026, 11, 1, 15, 0, tzinfo=UTC)

    async def _latest(_client, _key, series, *, fetched_at=None):
        obs = date(2026, 10, 30) if series == "DGS10" else date(2026, 8, 1)
        return FredObservation(series, 4.10, obs, evaluated)

    monkeypatch.setattr("agents.market.agent._fred_latest", _latest)
    result = await assess_market("fake-key", now=evaluated)
    assert result.evaluated_at is None
    assert "STALE_UNRATE" in result.reasons


@pytest.mark.asyncio
async def test_future_observation_date_is_data_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    fetched = datetime(2026, 9, 4, 15, 0, tzinfo=UTC)

    async def _latest(_client, _key, series, *, fetched_at=None):
        obs = fetched.date() + timedelta(days=1) if series == "DGS10" else fetched.date()
        return FredObservation(series, 4.10, obs, fetched_at or fetched)

    monkeypatch.setattr("agents.market.agent._fred_latest", _latest)
    result = await assess_market("fake-key", now=fetched)
    assert result.evaluated_at is None
    assert "FRED_OBSERVATION_DATE_INVALID" in result.reasons
    assert "DATA_BLOCKED" in result.reasons


@pytest.mark.asyncio
async def test_fred_series_reads_overlap_and_keep_provenance(monkeypatch):
    import asyncio

    entered = set()
    both = asyncio.Event()
    now = datetime(2026, 10, 2, 15, tzinfo=UTC)

    async def latest(client, key, series, *, fetched_at):
        entered.add(series)
        if len(entered) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), timeout=1)
        return FredObservation(series, 4.1, now.date(), fetched_at)

    monkeypatch.setattr("agents.market.agent._fred_latest", latest)
    result = await assess_market("test-key", now=now)
    assert entered == {"DGS10", "UNRATE"}
    assert result.evaluated_at == now and result.fetched_at == now
