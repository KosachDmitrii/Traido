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


def _cluster(levels: list[float], tolerance: float = 0.01, keep: int = 3) -> list[Decimal]:
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
    # prefer levels near latest price — caller sorts; here take last swings' means
    means = means[-keep:]
    return [Decimal(str(round(m, 4))) for m in means]


def support_resistance(
    highs: list[float],
    lows: list[float],
    keep: int = 3,
) -> tuple[list[Decimal], list[Decimal]]:
    support = _cluster(_swings(lows, "low"), keep=keep)
    resistance = _cluster(_swings(highs, "high"), keep=keep)
    return support, resistance
