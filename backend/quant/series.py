"""Shared bar series helpers for quant (float arrays for speed; Bar stays Decimal at edges)."""

from __future__ import annotations

from decimal import Decimal

from core.schemas import Bar


def closes(bars: list[Bar]) -> list[float]:
    return [float(b.close) for b in bars]


def highs(bars: list[Bar]) -> list[float]:
    return [float(b.high) for b in bars]


def lows(bars: list[Bar]) -> list[float]:
    return [float(b.low) for b in bars]


def opens(bars: list[Bar]) -> list[float]:
    return [float(b.open) for b in bars]


def volumes(bars: list[Bar]) -> list[float]:
    return [float(b.volume) for b in bars]


def last_n(values: list[float], n: int) -> list[float]:
    if n <= 0:
        return []
    return values[-n:]


def to_decimal(value: float | None, places: str = "0.0001") -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(round(value, 8))).quantize(Decimal(places))
