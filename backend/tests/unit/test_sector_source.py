"""Finnhub profile2 sector source — curated map first, vendor for the rest."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from core.enums import EarningsCheck, NewsCheck, SectorCheck, TradeAction
from core.schemas import PortfolioSnapshot, RiskLimits, TradeCandidate
from core.universe import Universe
from market_data.providers.sector import (
    SectorResolver,
    map_finnhub_industry,
    parse_profile_payload,
)
from risk.risk_engine import RiskContext, RiskEngine

_NOW = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
_KEY = "k" * 20


def _universe(*pairs: tuple[str, str]) -> Universe:
    return Universe(
        symbols=[s for s, _ in pairs],
        sectors={s: sector for s, sector in pairs},
    )


def test_industry_map_covers_the_eleven_groups() -> None:
    assert map_finnhub_industry("Technology") == "technology"
    assert map_finnhub_industry("Communication Services") == "communication"
    assert map_finnhub_industry("Consumer Cyclical") == "consumer_discretionary"
    assert map_finnhub_industry("Consumer Defensive") == "consumer_staples"
    assert map_finnhub_industry("Financial Services") == "financials"
    assert map_finnhub_industry("Healthcare") == "healthcare"
    assert map_finnhub_industry("Energy") == "energy"
    assert map_finnhub_industry("Industrials") == "industrials"
    assert map_finnhub_industry("Basic Materials") == "materials"
    assert map_finnhub_industry("Utilities") == "utilities"
    assert map_finnhub_industry("Real Estate") == "real_estate"


def test_an_unknown_industry_is_not_invented() -> None:
    assert map_finnhub_industry("Retail") is None
    assert map_finnhub_industry("") is None
    assert map_finnhub_industry(None) is None


def test_empty_profile_is_unclassified_not_unavailable() -> None:
    info = parse_profile_payload("ZZZZ", {})
    assert info.status is SectorCheck.UNCLASSIFIED
    assert info.sector is None


def test_mapped_profile_is_checked() -> None:
    info = parse_profile_payload("AAPL", {"finnhubIndustry": "Technology", "ticker": "AAPL"})
    assert info.status is SectorCheck.CHECKED
    assert info.sector == "technology"
    assert info.source == "finnhub"


@pytest.mark.asyncio
async def test_curated_universe_wins_over_finnhub() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"finnhubIndustry": "Energy"})

    resolver = SectorResolver(
        _KEY,
        universe=_universe(("PLTR", "technology")),
        transport=httpx.MockTransport(handler),
    )
    info = await resolver.resolve("PLTR", now=_NOW)
    assert info.sector == "technology"
    assert info.source == "universe"
    assert calls == 0


@pytest.mark.asyncio
async def test_finnhub_fills_a_name_outside_the_file() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "token" not in str(request.url).lower()
        assert request.headers.get("X-Finnhub-Token") == _KEY
        return httpx.Response(200, json={"finnhubIndustry": "Technology"})

    resolver = SectorResolver(
        _KEY,
        universe=_universe(("AAPL", "technology")),
        transport=httpx.MockTransport(handler),
    )
    info = await resolver.resolve("PLTR", now=_NOW)
    assert info.status is SectorCheck.CHECKED
    assert info.sector == "technology"
    assert info.source == "finnhub"


@pytest.mark.asyncio
async def test_empty_vendor_answer_is_unclassified() -> None:
    resolver = SectorResolver(
        _KEY,
        universe=_universe(),
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    info = await resolver.resolve("ZZZZ", now=_NOW)
    assert info.status is SectorCheck.UNCLASSIFIED
    assert info.sector is None


@pytest.mark.asyncio
async def test_missing_key_is_not_configured() -> None:
    resolver = SectorResolver(None, universe=_universe())
    info = await resolver.resolve("PLTR", now=_NOW)
    assert info.status is SectorCheck.NOT_CONFIGURED


@pytest.mark.asyncio
async def test_vendor_outage_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="no")

    resolver = SectorResolver(
        _KEY,
        universe=_universe(),
        transport=httpx.MockTransport(handler),
    )
    info = await resolver.resolve("PLTR", now=_NOW)
    assert info.status is SectorCheck.UNAVAILABLE
    assert "HTTP 503" in info.note


@pytest.mark.asyncio
async def test_success_is_cached_across_days() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"finnhubIndustry": "Healthcare"})

    resolver = SectorResolver(
        _KEY,
        universe=_universe(),
        transport=httpx.MockTransport(handler),
        ttl=timedelta(days=7),
    )
    first = await resolver.resolve("JNJ", now=_NOW)
    second = await resolver.resolve("JNJ", now=_NOW + timedelta(days=3))
    assert first.sector == second.sector == "healthcare"
    assert calls == 1


def test_engine_refuses_not_configured_and_unavailable() -> None:
    portfolio = PortfolioSnapshot(
        equity=Decimal(100_000),
        cash=Decimal(100_000),
        buying_power=Decimal(100_000),
        open_exposure=Decimal(0),
        open_positions=0,
        day_pnl=Decimal(0),
        week_pnl=Decimal(0),
        drawdown_pct=0.0,
        kill_switch=False,
    )
    candidate = TradeCandidate(
        symbol="PLTR",
        action=TradeAction.BUY,
        entry=Decimal(100),
        stop=Decimal(98),
        target=Decimal(104),
        confidence=0.8,
        risk_reward=2.0,
        reasons=["test"],
        strategy_version="test@1",
    )
    for status, code in (
        (SectorCheck.NOT_CONFIGURED, "SECTOR_NOT_CONFIGURED"),
        (SectorCheck.UNAVAILABLE, "SECTOR_UNAVAILABLE"),
    ):
        ctx = RiskContext(
            news=NewsCheck.CHECKED,
            earnings=EarningsCheck.CHECKED,
            sector_check=status,
            now=_NOW,
        )
        decision = RiskEngine().evaluate(candidate, portfolio, context=ctx)
        assert code in decision.reasons
