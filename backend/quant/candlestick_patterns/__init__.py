"""Candlestick pattern detectors — boolean flags on last bar / last 3 bars."""

from __future__ import annotations


def _body(o: float, c: float) -> float:
    return abs(c - o)


def _range(h: float, l: float) -> float:
    return max(h - l, 1e-12)


def doji(o: float, h: float, l: float, c: float, body_ratio: float = 0.1) -> bool:
    return _body(o, c) / _range(h, l) <= body_ratio


def hammer(o: float, h: float, l: float, c: float) -> bool:
    body = _body(o, c)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return lower >= body * 2 and upper <= body * 0.5 and body / _range(h, l) >= 0.1


def shooting_star(o: float, h: float, l: float, c: float) -> bool:
    body = _body(o, c)
    upper = h - max(o, c)
    lower = min(o, c) - l
    return upper >= body * 2 and lower <= body * 0.5 and body / _range(h, l) >= 0.1


def bullish_engulfing(o1: float, c1: float, o2: float, c2: float) -> bool:
    return c1 < o1 and c2 > o2 and o2 <= c1 and c2 >= o1


def bearish_engulfing(o1: float, c1: float, o2: float, c2: float) -> bool:
    return c1 > o1 and c2 < o2 and o2 >= c1 and c2 <= o1


def morning_star(
    o1: float,
    c1: float,
    o2: float,
    h2: float,
    l2: float,
    c2: float,
    o3: float,
    c3: float,
) -> bool:
    return c1 < o1 and doji(o2, h2, l2, c2, body_ratio=0.35) and c3 > o3 and c3 >= (o1 + c1) / 2


def evening_star(
    o1: float,
    c1: float,
    o2: float,
    h2: float,
    l2: float,
    c2: float,
    o3: float,
    c3: float,
) -> bool:
    return c1 > o1 and doji(o2, h2, l2, c2, body_ratio=0.35) and c3 < o3 and c3 <= (o1 + c1) / 2


def detect_candles(
    opens: list[float], highs: list[float], lows: list[float], closes: list[float]
) -> dict[str, bool]:
    if len(closes) < 1:
        return {
            "doji": False,
            "hammer": False,
            "shooting_star": False,
            "bullish_engulfing": False,
            "bearish_engulfing": False,
            "morning_star": False,
            "evening_star": False,
        }
    o, h, l, c = opens[-1], highs[-1], lows[-1], closes[-1]
    flags = {
        "doji": doji(o, h, l, c),
        "hammer": hammer(o, h, l, c),
        "shooting_star": shooting_star(o, h, l, c),
        "bullish_engulfing": False,
        "bearish_engulfing": False,
        "morning_star": False,
        "evening_star": False,
    }
    if len(closes) >= 2:
        flags["bullish_engulfing"] = bullish_engulfing(opens[-2], closes[-2], o, c)
        flags["bearish_engulfing"] = bearish_engulfing(opens[-2], closes[-2], o, c)
    if len(closes) >= 3:
        flags["morning_star"] = morning_star(
            opens[-3], closes[-3], opens[-2], highs[-2], lows[-2], closes[-2], o, c
        )
        flags["evening_star"] = evening_star(
            opens[-3], closes[-3], opens[-2], highs[-2], lows[-2], closes[-2], o, c
        )
    return flags
