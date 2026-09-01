"""
Relative strength versus a benchmark.

Absolute momentum is not enough: in a bull tape almost everything rises. What
distinguishes a genuine leader is outperforming the index, and continuing to
outperform when the index pulls back. The RS line making new highs while price
consolidates is one of the few signals that survives out of sample.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise

from core.schemas import Bar
from quant.series import closes

DEFAULT_BENCHMARK = "SPY"


@dataclass(frozen=True)
class RelativeStrengthProfile:
    benchmark: str
    rs_line: list[float]
    """Symbol price divided by benchmark price, rebased to 100 at the window start."""
    outperformance_pct: dict[int, float]
    """Symbol return minus benchmark return, in percentage points, by lookback."""
    rs_new_high: bool
    """RS line is at its highest value of the window — leadership confirmed."""
    rs_slope_pct: float | None
    """Percent change of the RS line over the recent window."""
    beta: float | None
    alpha_pct: float | None
    """Return not explained by benchmark exposure, over the medium window."""
    reasons: list[str]

    def score(self) -> int:
        """0-100. 50 means moving exactly with the benchmark."""
        parts: list[float] = []
        for lb in (21, 63, 126):
            v = self.outperformance_pct.get(lb)
            if v is not None:
                parts.append(max(0.0, min(100.0, 50 + v * 2.5)))
        if self.rs_slope_pct is not None:
            parts.append(max(0.0, min(100.0, 50 + self.rs_slope_pct * 3.0)))
        if not parts:
            return 50
        base = sum(parts) / len(parts)
        if self.rs_new_high:
            base += 8
        return round(max(0.0, min(100.0, base)))


def _returns(values: list[float]) -> list[float]:
    return [(b - a) / a for a, b in pairwise(values) if a > 0]


def _pct_change(values: list[float], lookback: int) -> float | None:
    if len(values) <= lookback or lookback <= 0:
        return None
    past = values[-(lookback + 1)]
    if past <= 0:
        return None
    return (values[-1] - past) / past * 100.0


def compute_beta(symbol_closes: list[float], benchmark_closes: list[float]) -> float | None:
    """OLS beta of symbol returns on benchmark returns."""
    n = min(len(symbol_closes), len(benchmark_closes))
    if n < 30:
        return None
    sr = _returns(symbol_closes[-n:])
    br = _returns(benchmark_closes[-n:])
    m = min(len(sr), len(br))
    if m < 20:
        return None
    sr, br = sr[-m:], br[-m:]
    b_mean = sum(br) / m
    s_mean = sum(sr) / m
    cov = sum((b - b_mean) * (s - s_mean) for b, s in zip(br, sr, strict=True)) / (m - 1)
    var = sum((b - b_mean) ** 2 for b in br) / (m - 1)
    if var == 0:
        return None
    return cov / var


def compute_relative_strength(
    bars: list[Bar],
    benchmark_bars: list[Bar],
    *,
    benchmark: str = DEFAULT_BENCHMARK,
    lookbacks: tuple[int, ...] = (21, 63, 126, 252),
    slope_window: int = 21,
) -> RelativeStrengthProfile:
    c = closes(bars)
    b = closes(benchmark_bars)
    n = min(len(c), len(b))
    reasons: list[str] = []

    if n < 25:
        return RelativeStrengthProfile(
            benchmark=benchmark.upper(),
            rs_line=[],
            outperformance_pct={},
            rs_new_high=False,
            rs_slope_pct=None,
            beta=None,
            alpha_pct=None,
            reasons=["Insufficient overlapping history for relative strength"],
        )

    cs, bs = c[-n:], b[-n:]
    base = cs[0] / bs[0] if bs[0] > 0 else 1.0
    rs_line = [(x / y) / base * 100.0 for x, y in zip(cs, bs, strict=True) if y > 0]

    outperf: dict[int, float] = {}
    for lb in lookbacks:
        s_ret, b_ret = _pct_change(cs, lb), _pct_change(bs, lb)
        if s_ret is not None and b_ret is not None:
            outperf[lb] = s_ret - b_ret

    rs_new_high = bool(rs_line) and rs_line[-1] >= max(rs_line) - 1e-9
    rs_slope = _pct_change(rs_line, slope_window) if len(rs_line) > slope_window else None

    beta = compute_beta(cs, bs)
    alpha = None
    medium_s, medium_b = _pct_change(cs, 63), _pct_change(bs, 63)
    if beta is not None and medium_s is not None and medium_b is not None:
        alpha = medium_s - beta * medium_b

    for lb, v in sorted(outperf.items()):
        if abs(v) >= 1.0:
            reasons.append(f"{lb}-bar outperformance {v:+.1f}pp vs {benchmark.upper()}")
    if rs_new_high:
        reasons.append(f"Relative strength line at new high vs {benchmark.upper()}")
    if beta is not None:
        reasons.append(f"Beta {beta:.2f}")
    if alpha is not None:
        reasons.append(f"Alpha {alpha:+.1f}% over 3 months")

    return RelativeStrengthProfile(
        benchmark=benchmark.upper(),
        rs_line=rs_line,
        outperformance_pct=outperf,
        rs_new_high=rs_new_high,
        rs_slope_pct=rs_slope,
        beta=beta,
        alpha_pct=alpha,
        reasons=reasons,
    )


def rank_by_relative_strength(
    scores: dict[str, int],
) -> dict[str, float]:
    """
    Convert raw RS scores into 0..100 percentile ranks across the universe.

    Cross-sectional ranking is what makes momentum work — absolute thresholds
    drift with the market, percentile ranks do not.
    """
    if not scores:
        return {}
    ordered = sorted(scores.items(), key=lambda kv: kv[1])
    n = len(ordered)
    if n == 1:
        return {ordered[0][0]: 100.0}
    return {sym: (i / (n - 1)) * 100.0 for i, (sym, _) in enumerate(ordered)}
