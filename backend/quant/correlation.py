"""
Return correlation.

Five positions in five different semiconductor names is one position with five
times the size. This module gives the Risk Engine the number it needs to refuse
that trade: how closely a candidate moves with what is already on the book.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from itertools import pairwise

from core.schemas import Bar
from quant.series import closes

MIN_OVERLAP = 30
"""Below this many shared observations a correlation estimate is not meaningful."""


def returns_from_bars(bars: list[Bar]) -> list[float]:
    c = closes(bars)
    return [(b - a) / a for a, b in pairwise(c) if a > 0]


def pearson(a: list[float], b: list[float]) -> float | None:
    """Pearson correlation over the overlapping tail of two return series."""
    n = min(len(a), len(b))
    if n < MIN_OVERLAP:
        return None
    x, y = a[-n:], b[-n:]
    mx, my = sum(x) / n, sum(y) / n
    cov = sum((i - mx) * (j - my) for i, j in zip(x, y, strict=True))
    vx = sum((i - mx) ** 2 for i in x)
    vy = sum((j - my) ** 2 for j in y)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


@dataclass(frozen=True)
class CorrelationMatrix:
    symbols: list[str]
    values: dict[tuple[str, str], float] = field(default_factory=dict)

    def get(self, a: str, b: str) -> float | None:
        a, b = a.upper(), b.upper()
        if a == b:
            return 1.0
        return self.values.get((a, b)) or self.values.get((b, a))

    def max_with(self, symbol: str, others: list[str]) -> tuple[str | None, float | None]:
        """Highest correlation between `symbol` and any of `others`."""
        best_sym: str | None = None
        best_val: float | None = None
        for other in others:
            v = self.get(symbol, other)
            if v is None:
                continue
            if best_val is None or v > best_val:
                best_sym, best_val = other.upper(), v
        return best_sym, best_val

    def average_pairwise(self) -> float | None:
        if not self.values:
            return None
        return sum(self.values.values()) / len(self.values)


def build_correlation_matrix(
    returns_by_symbol: Mapping[str, list[float]],
) -> CorrelationMatrix:
    symbols = sorted(s.upper() for s in returns_by_symbol)
    normalised = {s.upper(): v for s, v in returns_by_symbol.items()}
    values: dict[tuple[str, str], float] = {}
    for i, a in enumerate(symbols):
        for b in symbols[i + 1 :]:
            r = pearson(normalised[a], normalised[b])
            if r is not None:
                values[(a, b)] = r
    return CorrelationMatrix(symbols=symbols, values=values)


def correlation_matrix_from_bars(
    bars_by_symbol: Mapping[str, list[Bar]],
) -> CorrelationMatrix:
    return build_correlation_matrix(
        {sym: returns_from_bars(bars) for sym, bars in bars_by_symbol.items()}
    )


@dataclass(frozen=True)
class ConcentrationCheck:
    """Result of testing a candidate against the existing book."""

    candidate: str
    max_correlation: float | None
    most_correlated_symbol: str | None
    effective_positions: float | None
    """Correlation-adjusted position count. Well below the raw count means the
    book is really one bet wearing several tickers."""
    breaches: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.breaches


def effective_position_count(matrix: CorrelationMatrix, symbols: list[str]) -> float | None:
    """
    Diversification measure: n / (1 + (n-1) * average correlation).

    Ten positions all correlated 0.9 count as roughly 1.1 independent bets.
    """
    n = len(symbols)
    if n <= 1:
        return float(n)
    pairs = [
        v
        for i, a in enumerate(symbols)
        for b in symbols[i + 1 :]
        if (v := matrix.get(a, b)) is not None
    ]
    if not pairs:
        return None
    avg = sum(pairs) / len(pairs)
    denom = 1 + (n - 1) * max(avg, 0.0)
    if denom <= 0:
        return None
    return n / denom


def check_concentration(
    candidate: str,
    open_symbols: list[str],
    matrix: CorrelationMatrix,
    *,
    max_pair_correlation: float = 0.80,
    min_effective_positions: float = 2.0,
) -> ConcentrationCheck:
    """
    Decide whether adding `candidate` makes the book dangerously concentrated.

    Deterministic and side-effect free — the Risk Engine calls this and treats
    any breach as a hard reject.
    """
    candidate = candidate.upper()
    others = [s.upper() for s in open_symbols if s.upper() != candidate]
    sym, worst = matrix.max_with(candidate, others)

    breaches: list[str] = []
    if worst is not None and worst > max_pair_correlation:
        breaches.append("MAX_CORRELATION")

    effective = None
    if others:
        effective = effective_position_count(matrix, [*others, candidate])
        if effective is not None and len(others) >= 2 and effective < min_effective_positions:
            breaches.append("INSUFFICIENT_DIVERSIFICATION")

    return ConcentrationCheck(
        candidate=candidate,
        max_correlation=worst,
        most_correlated_symbol=sym,
        effective_positions=effective,
        breaches=breaches,
    )
