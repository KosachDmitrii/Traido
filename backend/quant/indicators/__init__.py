"""Deterministic technical indicators — no LLM."""

from __future__ import annotations


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    window = sum(values[:period])
    out[period - 1] = window / period
    for i in range(period, len(values)):
        window += values[i] - values[i - period]
        out[i] = window / period
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change >= 0:
            gains += change
        else:
            losses -= change
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))
    return out


def macd(
    values: list[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    fast_e = ema(values, fast)
    slow_e = ema(values, slow)
    line: list[float | None] = [None] * len(values)
    for i in range(len(values)):
        if fast_e[i] is not None and slow_e[i] is not None:
            line[i] = fast_e[i] - slow_e[i]  # type: ignore[operator]
    # signal EMA over macd line values (skip Nones)
    compact = [v for v in line if v is not None]
    sig_compact = ema(compact, signal)
    signal_line: list[float | None] = [None] * len(values)
    hist: list[float | None] = [None] * len(values)
    compact_i = 0
    for i, v in enumerate(line):
        if v is None:
            continue
        s = sig_compact[compact_i]
        signal_line[i] = s
        if s is not None:
            hist[i] = v - s
        compact_i += 1
    return line, signal_line, hist


def atr(
    high: list[float], low: list[float], close: list[float], period: int = 14
) -> list[float | None]:
    n = len(close)
    out: list[float | None] = [None] * n
    if n == 0:
        return out
    tr: list[float] = [high[0] - low[0]]
    for i in range(1, n):
        tr.append(
            max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i] - close[i - 1]),
            )
        )
    return sma(tr, period)


def bollinger(
    values: list[float],
    period: int = 20,
    num_std: float = 2.0,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    mid = sma(values, period)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = mid[i]
        if mean is None:
            continue
        var = sum((x - mean) ** 2 for x in window) / period
        std = var**0.5
        upper[i] = mean + num_std * std
        lower[i] = mean - num_std * std
    return upper, mid, lower


def vwap(
    high: list[float],
    low: list[float],
    close: list[float],
    volume: list[float],
) -> list[float | None]:
    out: list[float | None] = [None] * len(close)
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(len(close)):
        typical = (high[i] + low[i] + close[i]) / 3.0
        cum_pv += typical * volume[i]
        cum_v += volume[i]
        out[i] = None if cum_v == 0 else cum_pv / cum_v
    return out


def relative_volume(volume: list[float], lookback: int = 20) -> float | None:
    if len(volume) < lookback + 1:
        return None
    avg = sum(volume[-(lookback + 1) : -1]) / lookback
    if avg == 0:
        return None
    return volume[-1] / avg


def last(values: list[float | None]) -> float | None:
    for v in reversed(values):
        if v is not None:
            return v
    return None
