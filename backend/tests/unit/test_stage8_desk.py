"""Stage 8: price action, chart patterns, desk evaluation identity."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.enums import Timeframe
from core.schemas import Bar
from quant.backtesting.desk_strategy import DeskConfluenceStrategy
from quant.backtesting.service import resolve_strategy_kind
from quant.chart_patterns import detect_chart_patterns
from quant.engine import compute_features
from quant.price_action import detect_price_action
from strategy.registry import LIVE_STRATEGY_KEY


def _ohlc(
    n: int,
    *,
    start: float = 100.0,
    drift: float = 0.002,
    gap_at: int | None = None,
    gap_pct: float = 0.02,
) -> tuple[list[float], list[float], list[float], list[float]]:
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    px = start
    for i in range(n):
        o = px
        if gap_at is not None and i == gap_at:
            o = px * (1.0 + gap_pct)
        c = o * (1.0 + drift)
        h = max(o, c) * 1.005
        l = min(o, c) * 0.995
        opens.append(o)
        highs.append(h)
        lows.append(l)
        closes.append(c)
        px = c
    return opens, highs, lows, closes


def test_price_action_detects_gap_up() -> None:
    o, h, l, c = _ohlc(30, gap_at=29, gap_pct=0.03)
    pa = detect_price_action(o, h, l, c)
    assert pa.gap_up is True
    assert pa.gap_pct is not None and pa.gap_pct >= 0.015


def test_price_action_detects_breakout() -> None:
    o, h, l, c = _ohlc(40, drift=0.0)
    # Flat range then close through resistance.
    for i in range(20, 39):
        o[i] = h[i] = l[i] = c[i] = 100.0
    o[-1] = 100.0
    h[-1] = 106.0
    l[-1] = 99.5
    c[-1] = 105.5
    pa = detect_price_action(o, h, l, c, lookback=15)
    assert pa.breakout is True
    assert pa.resistance is not None


def test_chart_patterns_include_expanded_set() -> None:
    _o, h, l, c = _ohlc(50, drift=0.003)
    charts = detect_chart_patterns(h, l, c)
    assert "triangle_ascending" in charts
    assert "flag_bull" in charts
    assert "head_shoulders" in charts
    assert "inv_head_shoulders" in charts
    assert charts["structure"] in {"uptrend", "downtrend", "range"}


def test_engine_exposes_pa_flags() -> None:
    bars: list[Bar] = []
    ts0 = datetime(2024, 1, 2, tzinfo=UTC)
    px = Decimal(100)
    for i in range(80):
        nxt = px * Decimal("1.002")
        bars.append(
            Bar(
                symbol="TEST",
                timeframe=Timeframe.D1,
                ts=ts0 + timedelta(days=i),
                open=px,
                high=nxt * Decimal("1.01"),
                low=px * Decimal("0.99"),
                close=nxt,
                volume=Decimal(1_000_000),
                source="test",
            )
        )
        px = nxt
    snap = compute_features("TEST", Timeframe.D1, bars)
    assert "pa_breakout" in snap.indicators
    assert "pa_gap_up" in snap.indicators
    assert "pa_reasons" in snap.indicators


def test_desk_strategy_version_matches_registry() -> None:
    assert DeskConfluenceStrategy.version == LIVE_STRATEGY_KEY
    assert LIVE_STRATEGY_KEY.startswith("trader_desk@")


def test_resolve_strategy_kind_defaults_to_desk() -> None:
    kind, factory, grid = resolve_strategy_kind(None)
    assert kind == "desk"
    strat = factory({})
    assert strat.version == LIVE_STRATEGY_KEY
    assert "rsi_cap" in grid

    kind2, factory2, _ = resolve_strategy_kind("stub")
    assert kind2 == "stub"
    assert factory2({}).version.startswith("ema_trend_stub")
