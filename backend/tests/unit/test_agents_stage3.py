"""Stage 3 — agents + supervisor (no broker orders)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from agents.strategy.agent import propose_trade
from agents.supervisor.agent import Supervisor
from agents.technical.agent import assess_technical
from core.audit import InMemoryAudit
from core.config import Settings
from core.enums import MarketRegimeLabel, Timeframe, TradeAction
from core.schemas import (
    FeatureSnapshot,
    MarketAssessment,
    NewsAssessment,
    TechnicalAssessment,
)
from market_data.providers.fixture import FixtureMarketData
from quant.engine import compute_features

FIXTURE_NOW = datetime(2024, 12, 30, 21, 0, tzinfo=UTC)
"""The close of the last session in `tests/fixtures/bars`."""


@pytest.mark.asyncio
async def test_technical_assessment_from_fixture() -> None:
    md = FixtureMarketData()
    end = datetime(2025, 12, 31, tzinfo=UTC)
    start = datetime(2023, 1, 1, tzinfo=UTC)
    bars = await md.get_bars("AAPL", Timeframe.D1, start, end)
    snap = compute_features("AAPL", Timeframe.D1, bars)
    tech = assess_technical("AAPL", {Timeframe.D1: snap})
    assert 0 <= tech.score <= 100
    assert tech.symbol == "AAPL"
    assert tech.reasons


def test_strategy_emits_valid_buy_or_none() -> None:
    tech = TechnicalAssessment(
        symbol="AAPL",
        trend="bullish",
        score=85,
        reasons=["EMA stack"],
    )
    news = NewsAssessment(
        symbol="AAPL",
        sentiment="positive",
        score=70,
        reasons=["stub"],
    )
    market = MarketAssessment(
        regime=MarketRegimeLabel.BULLISH,
        score=70,
        risk_posture="risk_on",
        reasons=["stub"],
    )
    features = {
        Timeframe.D1: FeatureSnapshot(
            symbol="AAPL",
            timeframe=Timeframe.D1,
            computed_at=datetime.now(UTC),
            indicators={
                "close": 190.0,
                "atr_14": 3.0,
                "sma_20": 188.0,
                "rsi_14": 52.0,
                "ema50_above_ema200": True,
            },
            candlestick_patterns={},
            chart_patterns={"structure": "uptrend"},
            support=[Decimal(185)],
        )
    }
    cand = propose_trade("AAPL", tech, news, market, features, pipeline_run_id=uuid4())
    assert cand is not None
    assert cand.action == TradeAction.BUY
    assert cand.stop < cand.entry < cand.target
    assert cand.risk_reward >= 2.0
    assert "confluence" in cand.strategy_version or "confluence" in cand.reasons[0].lower()


def test_strategy_rejects_weak_technical() -> None:
    tech = TechnicalAssessment(symbol="AAPL", trend="neutral", score=40, reasons=["weak"])
    news = NewsAssessment(symbol="AAPL", sentiment="neutral", score=50, reasons=["n"])
    market = MarketAssessment(
        regime=MarketRegimeLabel.NEUTRAL,
        score=50,
        risk_posture="neutral",
        reasons=["n"],
    )
    features = {
        Timeframe.D1: FeatureSnapshot(
            symbol="AAPL",
            timeframe=Timeframe.D1,
            computed_at=datetime.now(UTC),
            indicators={"close": 190.0, "atr_14": 3.0},
            candlestick_patterns={},
            chart_patterns={},
        )
    }
    assert propose_trade("AAPL", tech, news, market, features) is None


@pytest.mark.asyncio
async def test_supervisor_scan_fixture_no_orders() -> None:
    audit = InMemoryAudit()
    settings = Settings(
        alpaca_api_key=None,
        alpaca_api_secret=None,
        finnhub_api_key=None,
        fred_api_key=None,
    )
    # Force fixture path via empty alpaca keys on a fresh Settings — factory uses FixtureMarketData
    supervisor = Supervisor(
        market_data=FixtureMarketData(),
        audit=audit,
        settings=settings,
        # The fixtures are a recorded slice of the tape, so the scan is run from
        # inside that slice. Against the wall clock they are years stale, and
        # the freshness check would refuse them — correctly, which is the point
        # of it, and uselessly for a test about what the agents produce.
        clock=lambda: FIXTURE_NOW,
    )
    result = await supervisor.scan_symbol(
        "AAPL",
        timeframes=(Timeframe.D1,),
        lookback_days=900,
    )
    assert result.status in {"completed", "no_candidate"}
    assert result.technical is not None
    assert result.news is not None
    assert result.market is not None
    assert result.news.sentiment in {"neutral", "positive", "negative", "mixed"}
    # Critical: Stage 3 never creates broker-facing payloads
    assert "order" not in result.model_dump_json().lower() or result.candidate is not None
    event_types = {e["event_type"] for e in audit.events}
    assert "ScanJobStarted" in event_types
    assert "FeaturesComputed" in event_types
    assert "TechnicalAssessmentReady" in event_types


@pytest.mark.asyncio
async def test_scan_api(client_app=None) -> None:
    from httpx import ASGITransport, AsyncClient

    from api.main import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/scan/AAPL", params=[("timeframe", "1d")])
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "AAPL"
        assert data["status"] in {"completed", "no_candidate", "failed"}
        health = await client.get("/health")
        assert health.json()["stage"] >= 3
