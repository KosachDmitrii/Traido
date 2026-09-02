"""Sector assessment — classification vs bars-based market assessment."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.enums import DataHealthStatus, Timeframe
from core.schemas import Bar
from trading.sector_assessment import (
    BenchmarkBarsSectorAssessment,
    MetadataSectorAssessment,
    assess_from_benchmark_bars,
    get_sector_assessment_port,
)
from trading.sector_classification import classify_symbol
from trading.sector_policy import BENCHMARK_MIN_BARS


def _bars(symbol: str, n: int, *, trend: float = 0.002, now: datetime | None = None) -> list[Bar]:
    """Synthetic daily bars. Positive trend → bullish-ish; negative → bearish."""
    end = now or datetime.now(UTC)
    out: list[Bar] = []
    price = 100.0
    for i in range(n):
        ts = end - timedelta(days=n - i)
        price = price * (1.0 + trend)
        px = Decimal(str(round(price, 4)))
        out.append(
            Bar(
                symbol=symbol,
                timeframe=Timeframe.D1,
                ts=ts,
                open=px,
                high=px * Decimal("1.01"),
                low=px * Decimal("0.99"),
                close=px,
                volume=Decimal("1e6"),
                source="test",
            )
        )
    return out


def test_static_classification_never_returns_tradable_long() -> None:
    cls = classify_symbol("NEM")
    assert cls.benchmark == "GDX"
    assert cls.sector == "materials"
    assert not hasattr(cls, "tradable_long") or "tradable_long" not in cls.model_fields


@pytest.mark.asyncio
async def test_metadata_alone_does_not_grant_tradable() -> None:
    port = MetadataSectorAssessment()
    result = await port.assess("AAPL")
    assert result.tradable_long is None
    assert result.data_status is DataHealthStatus.UNHEALTHY
    assert "SECTOR_ASSESSMENT_REQUIRES_BARS" in result.reason_codes or (
        "SECTOR_ASSESSMENT_MISSING" in result.reason_codes
    )


@pytest.mark.asyncio
async def test_unknown_symbol_blocked() -> None:
    port = MetadataSectorAssessment()
    result = await port.assess("ZZZZ")
    assert result.tradable_long is None
    assert "SECTOR_METADATA_MISSING" in result.reason_codes


@pytest.mark.asyncio
async def test_gold_miner_uses_gdx() -> None:
    nem = classify_symbol("NEM")
    aem = classify_symbol("AEM")
    assert nem.benchmark == aem.benchmark == "GDX"
    assert nem.industry == "gold_miners"


@pytest.mark.asyncio
async def test_lly_uses_xlv() -> None:
    assert classify_symbol("LLY").benchmark == "XLV"


@pytest.mark.asyncio
async def test_nem_gdx_pass() -> None:
    cls = classify_symbol("NEM")
    bars = _bars("GDX", BENCHMARK_MIN_BARS + 10, trend=0.004)
    result = assess_from_benchmark_bars(cls, bars)
    assert result.data_status is DataHealthStatus.HEALTHY
    assert result.tradable_long is True
    assert result.benchmark == "GDX"


@pytest.mark.asyncio
async def test_nem_gdx_fail() -> None:
    cls = classify_symbol("NEM")
    bars = _bars("GDX", BENCHMARK_MIN_BARS + 10, trend=-0.004)
    result = assess_from_benchmark_bars(cls, bars)
    assert result.data_status is DataHealthStatus.HEALTHY
    assert result.tradable_long is False
    assert "SECTOR_BLOCKED" in result.reason_codes


@pytest.mark.asyncio
async def test_nem_gdx_missing() -> None:
    cls = classify_symbol("NEM")
    result = assess_from_benchmark_bars(cls, [])
    assert result.tradable_long is None
    assert result.data_status is DataHealthStatus.UNHEALTHY


@pytest.mark.asyncio
async def test_nem_gdx_stale() -> None:
    cls = classify_symbol("NEM")
    now = datetime.now(UTC)
    bars = _bars("GDX", BENCHMARK_MIN_BARS + 5, trend=0.004, now=now - timedelta(days=10))
    result = assess_from_benchmark_bars(cls, bars, now=now)
    assert result.tradable_long is None
    assert "SECTOR_BENCHMARK_STALE" in result.reason_codes


@pytest.mark.asyncio
async def test_lly_xlv_pass_fail_missing_stale() -> None:
    cls = classify_symbol("LLY")
    now = datetime.now(UTC)
    ok = assess_from_benchmark_bars(cls, _bars("XLV", BENCHMARK_MIN_BARS + 10, trend=0.004), now=now)
    assert ok.tradable_long is True
    bad = assess_from_benchmark_bars(
        cls, _bars("XLV", BENCHMARK_MIN_BARS + 10, trend=-0.004), now=now
    )
    assert bad.tradable_long is False
    missing = assess_from_benchmark_bars(cls, None, now=now)
    assert missing.tradable_long is None
    stale = assess_from_benchmark_bars(
        cls,
        _bars("XLV", BENCHMARK_MIN_BARS + 5, trend=0.004, now=now - timedelta(days=10)),
        now=now,
    )
    assert stale.tradable_long is None


@pytest.mark.asyncio
async def test_production_port_is_benchmark_bars() -> None:
    port = get_sector_assessment_port()
    assert isinstance(port, BenchmarkBarsSectorAssessment)


@pytest.mark.asyncio
async def test_sector_label_without_tradable_is_data_blocked() -> None:
    from trading.market_gate import evaluate_market_gate
    from core.enums import AssessmentKind, MarketRegimeLabel
    from core.schemas import MarketAssessment

    market = MarketAssessment(
        kind=AssessmentKind.MARKET,
        regime=MarketRegimeLabel.RISK_ON,
        score=70,
        risk_posture="risk_on",
        reasons=["test"],
        evaluated_at=datetime.now(UTC),
        benchmark="SPY",
    )
    gate = evaluate_market_gate(
        market,
        sector_label="healthcare",
        sector_tradable=None,
        require_sector=True,
    )
    assert gate.status is DataHealthStatus.UNHEALTHY
    assert "SECTOR_ASSESSMENT_MISSING" in gate.reason_codes
    assert gate.tradable_long is False
