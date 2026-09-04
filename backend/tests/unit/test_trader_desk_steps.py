"""Trader desk step agents — each owns one professional-trader gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agents.trader.context import run_context
from agents.trader.setup import run_setup
from agents.trader.structure import run_structure
from agents.trader.types import TraderBundle
from core.enums import Timeframe
from core.schemas import Bar, FeatureSnapshot
from trading.entry_policy import reset_entry_policy_cache, set_entry_aggressiveness


@pytest.fixture(autouse=True)
def _strict_trader_policy(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the confirmation slider; candidate gates stay Medium regardless."""
    from trading import entry_policy

    path = tmp_path / "entry_policy.json"
    monkeypatch.setattr(entry_policy, "POLICY_PATH", path)
    monkeypatch.delenv("REDIS_URL", raising=False)
    reset_entry_policy_cache()
    set_entry_aggressiveness(0, actor="test")
    yield
    reset_entry_policy_cache()


def _snap(
    symbol: str = "AAPL",
    *,
    structure: str = "uptrend",
    ema_ok: bool = True,
    close: float = 100.0,
    sma20: float = 99.0,
    rsi: float = 50.0,
    atr: float = 2.0,
) -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol=symbol,
        timeframe=Timeframe.D1,
        computed_at=datetime.now(UTC),
        indicators={
            "close": close,
            "sma_20": sma20,
            "rsi_14": rsi,
            "atr_14": atr,
            "ema50_above_ema200": ema_ok,
            "avg_dollar_volume": 50_000_000.0,
        },
        candlestick_patterns={},
        chart_patterns={"structure": structure},
    )


def test_structure_rejects_downtrend() -> None:
    bundle = TraderBundle(symbol="AAPL")
    bundle.features[Timeframe.D1] = _snap(structure="downtrend", ema_ok=False)
    step = run_structure(bundle)
    assert step.ok is False
    assert "STRUCTURE_REJECT" in step.reasons


def test_structure_allows_range_at_every_slider_step() -> None:
    """Candidate policy is Medium — range is allowed regardless of the slider."""
    for level in (0, 25, 50, 75, 100):
        set_entry_aggressiveness(level, actor="test")
        bundle = TraderBundle(symbol="AAPL")
        bundle.features[Timeframe.D1] = _snap(structure="range", ema_ok=True)
        step = run_structure(bundle)
        assert step.ok is True, (level, step.reasons)


def test_structure_allows_range_when_medium() -> None:
    set_entry_aggressiveness(50, actor="test")
    bundle = TraderBundle(symbol="AAPL")
    bundle.features[Timeframe.D1] = _snap(structure="range", ema_ok=True)
    step = run_structure(bundle)
    assert step.ok is True, step.reasons


def test_structure_still_requires_ema_stack() -> None:
    set_entry_aggressiveness(75, actor="test")
    bundle = TraderBundle(symbol="AAPL")
    bundle.features[Timeframe.D1] = _snap(structure="range", ema_ok=False)
    step = run_structure(bundle)
    assert step.ok is False
    assert "STRUCTURE_REJECT" in step.reasons


def test_five_desk_steps_share_candidate_rsi_cap() -> None:
    from agents.trader.policy import trader_gates_for
    from trading.entry_policy import thresholds_for

    caps = [trader_gates_for(thresholds_for(a)).rsi_overbought for a in (0, 25, 50, 75, 100)]
    assert len(set(caps)) == 1
    assert caps[0] == pytest.approx(74.0)


def test_structure_passes_uptrend_stack() -> None:
    bundle = TraderBundle(symbol="AAPL")
    bundle.features[Timeframe.D1] = _snap()
    step = run_structure(bundle)
    assert step.ok is True
    assert bundle.technical is not None
    assert bundle.technical.trend == "bullish"


def test_setup_rejects_chase() -> None:
    bundle = TraderBundle(symbol="AAPL")
    bundle.features[Timeframe.D1] = _snap(close=110.0, sma20=100.0)
    step = run_setup(bundle)
    assert step.ok is False
    assert "SETUP_CHASE" in step.reasons


def test_setup_rejects_rsi_above_candidate_cap() -> None:
    bundle = TraderBundle(symbol="AAPL")
    bundle.features[Timeframe.D1] = _snap(close=100.5, sma20=100.0, rsi=75.0)
    step = run_setup(bundle)
    assert step.ok is False
    assert "SETUP_RSI_HIGH" in step.reasons


def test_setup_allows_rsi_72_on_candidate_policy() -> None:
    bundle = TraderBundle(symbol="AAPL")
    bundle.features[Timeframe.D1] = _snap(close=100.5, sma20=100.0, rsi=72.0)
    step = run_setup(bundle)
    assert step.ok is True, step.reasons


def test_setup_allows_rsi_72_when_softer() -> None:
    set_entry_aggressiveness(75, actor="test")
    bundle = TraderBundle(symbol="AAPL")
    bundle.features[Timeframe.D1] = _snap(close=100.5, sma20=100.0, rsi=72.0)
    step = run_setup(bundle)
    assert step.ok is True, step.reasons


def test_setup_accepts_pullback() -> None:
    bundle = TraderBundle(symbol="AAPL")
    bundle.features[Timeframe.D1] = _snap(close=100.5, sma20=100.0, rsi=48.0)
    step = run_setup(bundle)
    assert step.ok is True


def test_setup_rejects_missing_sma20() -> None:
    bundle = TraderBundle(symbol="AAPL")
    snap = _snap(close=100.5, sma20=100.0, rsi=48.0)
    snap.indicators.pop("sma_20", None)
    bundle.features[Timeframe.D1] = snap
    step = run_setup(bundle)
    assert step.ok is False
    assert "SETUP_NO_SMA20" in step.reasons


@pytest.mark.asyncio
async def test_context_allows_neutral_spy() -> None:
    """Range / mixed SPY must not freeze the desk — only hostile regimes block."""

    class _MD:
        async def get_bars(self, symbol, timeframe, start, end):
            assert symbol == "SPY"
            base = datetime.now(UTC) - timedelta(days=120)
            bars: list[Bar] = []
            for i in range(100):
                px = 100.0 + (i % 7) * 0.1
                ts = base + timedelta(days=i)
                bars.append(
                    Bar(
                        symbol="SPY",
                        timeframe=Timeframe.D1,
                        ts=ts,
                        open=Decimal(str(px)),
                        high=Decimal(str(px + 0.5)),
                        low=Decimal(str(px - 0.5)),
                        close=Decimal(str(px)),
                        volume=Decimal(1_000_000),
                        source="test",
                    )
                )
            return bars

    bundle = TraderBundle(symbol="AAPL")
    step = await run_context(bundle, _MD())  # type: ignore[arg-type]
    assert step.ok is True, step.reasons
    assert bundle.market is not None
    assert bundle.market.risk_posture in {"neutral", "risk_on"}


@pytest.mark.asyncio
async def test_context_blocks_bearish_spy() -> None:
    class _MD:
        async def get_bars(self, symbol, timeframe, start, end):
            base = datetime.now(UTC) - timedelta(days=250)
            bars: list[Bar] = []
            px = 200.0
            for i in range(220):
                px = 200.0 - i * 0.4  # sustained decline
                ts = base + timedelta(days=i)
                bars.append(
                    Bar(
                        symbol="SPY",
                        timeframe=Timeframe.D1,
                        ts=ts,
                        open=Decimal(str(px + 0.2)),
                        high=Decimal(str(px + 0.5)),
                        low=Decimal(str(px - 0.5)),
                        close=Decimal(str(px)),
                        volume=Decimal(1_000_000),
                        source="test",
                    )
                )
            return bars

    bundle = TraderBundle(symbol="AAPL")
    step = await run_context(bundle, _MD())  # type: ignore[arg-type]
    assert step.ok is False
    assert "CONTEXT_NOT_TRADABLE" in step.reasons
