"""Stage 1 — indicators, patterns, fixture market data."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.enums import Timeframe
from core.schemas import Bar
from market_data.providers.fixture import FixtureMarketData
from quant.candlestick_patterns import bullish_engulfing, doji, hammer
from quant.engine import compute_features
from quant.indicators import ema, macd, rsi, sma


def test_sma_known_window() -> None:
    values = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert sma(values, 3) == [None, None, 2.0, 3.0, 4.0]


def test_ema_length_and_last() -> None:
    values = [float(i) for i in range(1, 21)]
    out = ema(values, 5)
    assert out[4] == pytest.approx(3.0)
    assert out[-1] is not None
    assert out[-1] > out[4]  # type: ignore[operator]


def test_rsi_bounds_on_trend() -> None:
    up = [100 + i for i in range(30)]
    r = rsi(up, 14)
    assert r[-1] is not None
    assert r[-1] > 70


def test_macd_produces_histogram() -> None:
    values = [100 + (i * 0.5) for i in range(60)]
    line, signal, hist = macd(values)
    assert last_non_null(hist) is not None
    assert last_non_null(line) is not None
    assert last_non_null(signal) is not None


def last_non_null(xs: list[float | None]) -> float | None:
    for v in reversed(xs):
        if v is not None:
            return v
    return None


def test_candlestick_helpers() -> None:
    assert doji(10, 10.2, 9.8, 10.01)
    assert hammer(10, 10.2, 8.5, 10.5)
    assert bullish_engulfing(10.5, 10.0, 9.9, 10.6)


@pytest.mark.asyncio
async def test_compute_features_from_fixture_daily() -> None:
    md = FixtureMarketData()
    end = datetime(2025, 12, 31, tzinfo=UTC)
    start = datetime(2023, 1, 1, tzinfo=UTC)
    bars = await md.get_bars("AAPL", Timeframe.D1, start, end)
    assert len(bars) >= 150
    snap = compute_features("AAPL", Timeframe.D1, bars)
    assert snap.symbol == "AAPL"
    assert snap.indicators["rsi_14"] is not None
    assert snap.indicators["ema_50"] is not None
    assert "doji" in snap.candlestick_patterns
    assert snap.chart_patterns["structure"] in {"uptrend", "downtrend", "range"}


@pytest.mark.asyncio
async def test_fixture_aggregates_4h() -> None:
    md = FixtureMarketData()
    end = datetime(2025, 8, 1, tzinfo=UTC)
    start = datetime(2025, 6, 1, tzinfo=UTC)
    bars = await md.get_bars("AAPL", Timeframe.H4, start, end)
    assert len(bars) > 10
    assert all(b.timeframe == Timeframe.H4 for b in bars)
    for b in bars:
        assert b.low <= b.open <= b.high
        assert b.low <= b.close <= b.high


@pytest.mark.asyncio
async def test_15m_features_compute() -> None:
    md = FixtureMarketData()
    end = datetime(2025, 9, 1, tzinfo=UTC)
    start = datetime(2025, 7, 1, tzinfo=UTC)
    bars = await md.get_bars("AAPL", Timeframe.M15, start, end)
    assert len(bars) > 50
    snap = compute_features("AAPL", Timeframe.M15, bars)
    assert snap.indicators["close"] is not None
    assert isinstance(snap.candlestick_patterns["bullish_engulfing"], bool)


def test_bar_model_rejects_bad_symbol() -> None:
    with pytest.raises(ValidationError):
        Bar(
            symbol="",
            timeframe=Timeframe.D1,
            ts=datetime.now(UTC),
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(1),
            volume=Decimal(1),
            source="t",
        )
