"""Simple chart structure heuristics (not ML)."""

from __future__ import annotations


def _swing_highs(highs: list[float], left: int = 2, right: int = 2) -> list[int]:
    idx: list[int] = []
    for i in range(left, len(highs) - right):
        window = highs[i - left : i + right + 1]
        if highs[i] == max(window) and window.count(highs[i]) == 1:
            idx.append(i)
    return idx


def _swing_lows(lows: list[float], left: int = 2, right: int = 2) -> list[int]:
    idx: list[int] = []
    for i in range(left, len(lows) - right):
        window = lows[i - left : i + right + 1]
        if lows[i] == min(window) and window.count(lows[i]) == 1:
            idx.append(i)
    return idx


def detect_chart_patterns(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    tolerance: float = 0.015,
) -> dict[str, bool | str | None]:
    result: dict[str, bool | str | None] = {
        "double_top": False,
        "double_bottom": False,
        "higher_highs": False,
        "higher_lows": False,
        "lower_highs": False,
        "lower_lows": False,
        "structure": None,
    }
    if len(closes) < 10:
        return result

    sh = _swing_highs(highs)
    sl = _swing_lows(lows)
    if len(sh) >= 2:
        a, b = highs[sh[-2]], highs[sh[-1]]
        if abs(a - b) / ((a + b) / 2) <= tolerance and closes[-1] < min(a, b):
            result["double_top"] = True
        result["higher_highs"] = b > a * (1 + tolerance / 2)
        result["lower_highs"] = b < a * (1 - tolerance / 2)
    if len(sl) >= 2:
        a, b = lows[sl[-2]], lows[sl[-1]]
        if abs(a - b) / ((a + b) / 2) <= tolerance and closes[-1] > max(a, b):
            result["double_bottom"] = True
        result["higher_lows"] = b > a * (1 + tolerance / 2)
        result["lower_lows"] = b < a * (1 - tolerance / 2)

    if result["higher_highs"] and result["higher_lows"]:
        result["structure"] = "uptrend"
    elif result["lower_highs"] and result["lower_lows"]:
        result["structure"] = "downtrend"
    else:
        result["structure"] = "range"
    return result
