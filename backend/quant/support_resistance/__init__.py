"""Support / resistance from swing points."""

from __future__ import annotations

from decimal import Decimal


def _swings(values: list[float], mode: str, left: int = 2, right: int = 2) -> list[float]:
    levels: list[float] = []
    for i in range(left, len(values) - right):
        window = values[i - left : i + right + 1]
        if mode == "high" and values[i] == max(window):
            levels.append(values[i])
        if mode == "low" and values[i] == min(window):
            levels.append(values[i])
    return levels


def _cluster(levels: list[float], tolerance: float = 0.01, keep: int | None = 3) -> list[Decimal]:
    if not levels:
        return []
    levels = sorted(levels)
    clusters: list[list[float]] = [[levels[0]]]
    for level in levels[1:]:
        if abs(level - clusters[-1][-1]) / level <= tolerance:
            clusters[-1].append(level)
        else:
            clusters.append([level])
    means = [sum(c) / len(c) for c in clusters]
    if keep is not None:
        means = means[-keep:]
    return [Decimal(str(round(m, 4))) for m in means]


def support_resistance(
    highs: list[float],
    lows: list[float],
    keep: int = 3,
    *,
    reference_price: float | None = None,
) -> tuple[list[Decimal], list[Decimal]]:
    # Keep the old unanchored API for callers without a close, but production
    # features must select proximity BEFORE truncating the level population.
    if reference_price is None:
        return _cluster(_swings(lows, "low"), keep=keep), _cluster(
            _swings(highs, "high"), keep=keep
        )
    if keep <= 0:
        return [], []
    anchor = Decimal(str(reference_price))
    if not anchor.is_finite() or anchor <= 0:
        raise ValueError("INVALID_LEVEL_REFERENCE_PRICE")
    supports = _cluster(_swings(lows, "low"), keep=None)
    resistances = _cluster(_swings(highs, "high"), keep=None)
    support = [p for p in supports if p < anchor][-keep:]
    resistance = [p for p in resistances if p > anchor][:keep]
    return support, resistance
