"""Momentum, volatility, relative strength, correlation, regime, and tradability filters."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

import pytest

from core.enums import MarketRegimeLabel, Timeframe
from core.schemas import Bar
from quant.correlation import (
    build_correlation_matrix,
    check_concentration,
    correlation_matrix_from_bars,
    effective_position_count,
    pearson,
)
from quant.filters import TradabilityLimits, check_tradability
from quant.market_regime import annualised_volatility_pct, classify, segment_by_regime
from quant.momentum import compute_momentum, rate_of_change
from quant.relative_strength import (
    compute_beta,
    compute_relative_strength,
    rank_by_relative_strength,
)
from quant.volatility import average_dollar_volume, compute_volatility

_START = datetime(2022, 1, 3, 14, 30, tzinfo=UTC)


def _series(
    closes: list[float],
    *,
    symbol: str = "TEST",
    volume: float = 2_000_000,
    end: datetime | None = None,
    range_pct: float = 0.01,
) -> list[Bar]:
    end = end or datetime.now(UTC)
    n = len(closes)
    bars: list[Bar] = []
    for i, c in enumerate(closes):
        ts = end - timedelta(days=(n - 1 - i))
        bars.append(
            Bar(
                symbol=symbol,
                timeframe=Timeframe.D1,
                ts=ts,
                open=Decimal(str(round(c, 4))),
                high=Decimal(str(round(c * (1 + range_pct), 4))),
                low=Decimal(str(round(c * (1 - range_pct), 4))),
                close=Decimal(str(round(c, 4))),
                volume=Decimal(str(volume)),
                source="synthetic",
            )
        )
    return bars


def _trend(n: int, daily: float = 0.001, start: float = 100.0) -> list[float]:
    return [start * ((1 + daily) ** i) for i in range(n)]


def _oscillating(
    n: int, daily: float = 0.001, swing: float = 0.04, period: int = 40
) -> list[float]:
    return [
        100.0 * ((1 + daily) ** i) * (1 + swing * math.sin(2 * math.pi * i / period))
        for i in range(n)
    ]


# ── Momentum ─────────────────────────────────────────────────────────────────


def test_rate_of_change_matches_hand_calculation() -> None:
    assert rate_of_change([100, 110], 1) == pytest.approx(10.0)
    assert rate_of_change([100], 5) is None


def test_momentum_positive_in_uptrend_negative_in_downtrend() -> None:
    up = compute_momentum(_series(_trend(300, 0.002)))
    down = compute_momentum(_series(_trend(300, -0.002)))
    assert up.roc[63] > 0 > down.roc[63]
    assert up.score() > down.score()


def test_momentum_12_1_excludes_the_recent_month() -> None:
    closes = _trend(300, 0.001)
    # A spike confined to the last 21 bars must not move the 12-1 reading.
    spiked = [*closes[:-21], *[c * 1.5 for c in closes[-21:]]]
    assert compute_momentum(_series(closes)).momentum_12_1 == pytest.approx(
        compute_momentum(_series(spiked)).momentum_12_1
    )


def test_risk_adjusted_momentum_prefers_the_smoother_path() -> None:
    smooth = compute_momentum(_series(_trend(300, 0.001)))
    choppy = compute_momentum(_series(_oscillating(300, 0.001, swing=0.10, period=10)))
    assert smooth.risk_adjusted is not None and choppy.risk_adjusted is not None
    assert smooth.risk_adjusted > choppy.risk_adjusted


def test_momentum_score_is_bounded() -> None:
    for daily in (-0.05, -0.001, 0.0, 0.001, 0.05):
        score = compute_momentum(_series(_trend(300, daily))).score()
        assert 0 <= score <= 100


def test_momentum_handles_short_history() -> None:
    profile = compute_momentum(_series(_trend(10)))
    assert profile.roc == {}
    assert profile.score() == 50


# ── Volatility ───────────────────────────────────────────────────────────────


def test_atr_pct_rises_with_range() -> None:
    calm = compute_volatility(_series(_oscillating(200, 0.0, swing=0.01)))
    wild = compute_volatility(_series(_oscillating(200, 0.0, swing=0.15)))
    assert calm.atr_pct is not None and wild.atr_pct is not None
    assert wild.atr_pct > calm.atr_pct


def test_stop_distance_scales_with_atr_multiple() -> None:
    profile = compute_volatility(_series(_oscillating(200)))
    assert profile.stop_distance_pct(2.0) == pytest.approx(profile.stop_distance_pct(1.0) * 2)


def test_parkinson_estimator_is_positive() -> None:
    profile = compute_volatility(_series(_oscillating(200)))
    assert profile.parkinson_vol_annual_pct is not None
    assert profile.parkinson_vol_annual_pct > 0


def test_average_dollar_volume_uses_price_times_volume() -> None:
    bars = _series([100.0] * 30, volume=1_000_000)
    assert average_dollar_volume(bars, 20) == pytest.approx(100_000_000)


# ── Relative strength ────────────────────────────────────────────────────────


def test_leader_outperforms_benchmark() -> None:
    leader = _series(_trend(300, 0.002))
    bench = _series(_trend(300, 0.0005), symbol="SPY")
    rs = compute_relative_strength(leader, bench)
    assert rs.outperformance_pct[63] > 0
    assert rs.rs_new_high is True
    assert rs.score() > 50


def test_laggard_underperforms_benchmark() -> None:
    laggard = _series(_trend(300, -0.001))
    bench = _series(_trend(300, 0.001), symbol="SPY")
    rs = compute_relative_strength(laggard, bench)
    assert rs.outperformance_pct[63] < 0
    assert rs.score() < 50


def test_beta_of_a_doubled_series_is_two() -> None:
    bench = _trend(300, 0.001)
    levered = [100.0]
    for a, b in pairwise(bench):
        levered.append(levered[-1] * (1 + 2 * ((b - a) / a)))
    assert compute_beta(levered, bench) == pytest.approx(2.0, abs=0.05)


def test_relative_strength_degrades_gracefully_without_overlap() -> None:
    rs = compute_relative_strength(_series(_trend(10)), _series(_trend(10), symbol="SPY"))
    assert rs.rs_line == []
    assert rs.score() == 50


def test_rank_converts_scores_to_percentiles() -> None:
    ranks = rank_by_relative_strength({"A": 10, "B": 50, "C": 90})
    assert ranks["A"] == 0.0
    assert ranks["C"] == 100.0
    assert ranks["B"] == pytest.approx(50.0)


# ── Correlation ──────────────────────────────────────────────────────────────


def test_identical_series_correlate_perfectly() -> None:
    r = [0.01, -0.02, 0.03, -0.01] * 10
    assert pearson(r, r) == pytest.approx(1.0)


def test_inverse_series_correlate_negatively() -> None:
    r = [0.01, -0.02, 0.03, -0.01] * 10
    assert pearson(r, [-x for x in r]) == pytest.approx(-1.0)


def test_correlation_requires_minimum_overlap() -> None:
    assert pearson([0.01] * 5, [0.01] * 5) is None


def test_matrix_lookup_is_order_independent() -> None:
    r = [0.01, -0.02, 0.03, -0.01] * 10
    m = build_correlation_matrix({"AAA": r, "BBB": r})
    assert m.get("AAA", "BBB") == m.get("BBB", "AAA")
    assert m.get("AAA", "AAA") == 1.0


def test_effective_position_count_collapses_when_everything_correlates() -> None:
    r = [0.01, -0.02, 0.03, -0.01] * 10
    m = build_correlation_matrix({"A": r, "B": r, "C": r})
    effective = effective_position_count(m, ["A", "B", "C"])
    assert effective is not None and effective < 1.5


def test_concentration_blocks_a_duplicate_position() -> None:
    r = [0.01, -0.02, 0.03, -0.01] * 10
    m = build_correlation_matrix({"NVDA": r, "AMD": r})
    check = check_concentration("NVDA", ["AMD"], m, max_pair_correlation=0.8)
    assert not check.ok
    assert "MAX_CORRELATION" in check.breaches
    assert check.most_correlated_symbol == "AMD"


def test_concentration_allows_an_uncorrelated_position() -> None:
    a = [0.01, -0.02, 0.03, -0.01] * 10
    b = [0.02, 0.01, -0.03, 0.005] * 10
    m = build_correlation_matrix({"AAA": a, "BBB": b})
    assert check_concentration("AAA", ["BBB"], m, max_pair_correlation=0.95).ok


def test_matrix_from_bars_round_trips() -> None:
    bars = {"A": _series(_oscillating(120)), "B": _series(_oscillating(120))}
    m = correlation_matrix_from_bars(bars)
    assert m.get("A", "B") == pytest.approx(1.0, abs=0.01)


# ── Regime ───────────────────────────────────────────────────────────────────


def test_sustained_uptrend_reads_bullish() -> None:
    assert classify(_series(_trend(300, 0.002))).label is MarketRegimeLabel.BULLISH


def test_sustained_downtrend_reads_bearish() -> None:
    assert classify(_series(_trend(300, -0.002))).label is MarketRegimeLabel.BEARISH


def test_bearish_regime_is_not_tradable_long() -> None:
    assert classify(_series(_trend(300, -0.002))).is_tradable_long is False


def test_flat_tape_reads_neutral() -> None:
    assert classify(_series([100.0] * 300)).label is MarketRegimeLabel.NEUTRAL


def test_regime_handles_short_history() -> None:
    snapshot = classify(_series([100.0] * 5))
    assert snapshot.label is MarketRegimeLabel.NEUTRAL
    assert "Not enough bars" in snapshot.reasons[0]


def test_segments_cover_history_without_tiny_slivers() -> None:
    closes = [*_trend(200, 0.003), *_trend(200, -0.003, start=182.0)]
    segments = segment_by_regime(_series(closes), window=60, min_segment=30)
    assert segments
    assert all(s.length >= 30 for s in segments)
    assert all(s.start_index < s.end_index for s in segments)


def test_annualised_volatility_is_positive_for_moving_prices() -> None:
    v = annualised_volatility_pct(_series(_oscillating(200, swing=0.05)))
    assert v is not None and v > 0


# ── Tradability filters ──────────────────────────────────────────────────────


def test_liquid_large_cap_passes() -> None:
    bars = _series(_oscillating(200, swing=0.03), volume=5_000_000)
    assert check_tradability("AAPL", bars).passed


def test_penny_stock_is_rejected() -> None:
    bars = _series([2.0 * (1 + 0.01 * math.sin(i / 5)) for i in range(200)], volume=5_000_000)
    assert "PRICE_TOO_LOW" in check_tradability("PENNY", bars).rejections


def test_illiquid_name_is_rejected() -> None:
    bars = _series(_oscillating(200, swing=0.03), volume=100)
    assert "INSUFFICIENT_LIQUIDITY" in check_tradability("THIN", bars).rejections


def test_stale_data_is_rejected() -> None:
    bars = _series(
        _oscillating(200, swing=0.03),
        volume=5_000_000,
        end=datetime.now(UTC) - timedelta(days=30),
    )
    assert "STALE_DATA" in check_tradability("OLD", bars).rejections


def test_dead_flat_name_is_rejected_for_lack_of_range() -> None:
    bars = _series([100.0] * 200, volume=5_000_000, range_pct=0.0005)
    assert "VOLATILITY_TOO_LOW" in check_tradability("FLAT", bars).rejections


def test_constant_volatility_is_not_mistaken_for_a_blow_off() -> None:
    """A steady ATR has no percentile extremes — it must not read as high volatility."""
    profile = compute_volatility(_series([100.0] * 200))
    assert profile.vol_percentile == pytest.approx(0.5)
    assert not profile.expansion
    assert not profile.squeeze


def test_target_inside_cost_is_rejected() -> None:
    bars = _series(_oscillating(200, swing=0.03), volume=5_000_000)
    price = bars[-1].close
    result = check_tradability("AAPL", bars, target=price * Decimal("1.0001"))
    assert "EDGE_BELOW_COST_THRESHOLD" in result.rejections
    assert result.edge_to_cost_ratio is not None


def test_generous_target_clears_the_cost_gate() -> None:
    bars = _series(_oscillating(200, swing=0.03), volume=5_000_000)
    result = check_tradability("AAPL", bars, target=bars[-1].close * Decimal("1.08"))
    assert "EDGE_BELOW_COST_THRESHOLD" not in result.rejections


def test_short_history_is_rejected_immediately() -> None:
    result = check_tradability("NEW", _series(_trend(10)))
    assert result.rejections == ["INSUFFICIENT_HISTORY"]


def test_limits_are_configurable() -> None:
    bars = _series(_oscillating(200, swing=0.03), volume=5_000_000)
    strict = TradabilityLimits(min_avg_dollar_volume=1e12)
    assert "INSUFFICIENT_LIQUIDITY" in check_tradability("AAPL", bars, limits=strict).rejections
