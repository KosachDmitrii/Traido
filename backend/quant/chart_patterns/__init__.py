"""Simple chart structure heuristics (not ML). Stage 8 expands beyond DT/DB."""

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
        "triangle_ascending": False,
        "triangle_descending": False,
        "flag_bull": False,
        "flag_bear": False,
        "head_shoulders": False,
        "inv_head_shoulders": False,
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

    # Ascending triangle: flat highs, rising lows.
    if len(sh) >= 2 and len(sl) >= 2:
        h1, h2 = highs[sh[-2]], highs[sh[-1]]
        l1, l2 = lows[sl[-2]], lows[sl[-1]]
        flat_highs = abs(h1 - h2) / ((h1 + h2) / 2) <= tolerance * 1.5
        rising_lows = l2 > l1 * (1 + tolerance / 2)
        falling_highs = h2 < h1 * (1 - tolerance / 2)
        flat_lows = abs(l1 - l2) / ((l1 + l2) / 2) <= tolerance * 1.5
        if flat_highs and rising_lows:
            result["triangle_ascending"] = True
        if flat_lows and falling_highs:
            result["triangle_descending"] = True

    # Flag: sharp impulse then tight counter-move (last 8 bars vs prior 8).
    if len(closes) >= 16:
        prior = closes[-16:-8]
        recent = closes[-8:]
        impulse = (prior[-1] - prior[0]) / prior[0] if prior[0] else 0.0
        digest = (max(recent) - min(recent)) / recent[0] if recent[0] else 1.0
        if impulse > 0.04 and digest < 0.025 and closes[-1] >= recent[0]:
            result["flag_bull"] = True
        if impulse < -0.04 and digest < 0.025 and closes[-1] <= recent[0]:
            result["flag_bear"] = True

    # Head & shoulders / inverse — three swings, middle extreme.
    if len(sh) >= 3:
        left, head, right = highs[sh[-3]], highs[sh[-2]], highs[sh[-1]]
        if head > left * (1 + tolerance) and head > right * (1 + tolerance):
            if abs(left - right) / ((left + right) / 2) <= tolerance * 2:
                result["head_shoulders"] = True
    if len(sl) >= 3:
        left, head, right = lows[sl[-3]], lows[sl[-2]], lows[sl[-1]]
        if head < left * (1 - tolerance) and head < right * (1 - tolerance):
            if abs(left - right) / ((left + right) / 2) <= tolerance * 2:
                result["inv_head_shoulders"] = True

    if result["higher_highs"] and result["higher_lows"]:
        result["structure"] = "uptrend"
    elif result["lower_highs"] and result["lower_lows"]:
        result["structure"] = "downtrend"
    else:
        result["structure"] = "range"
    return result
