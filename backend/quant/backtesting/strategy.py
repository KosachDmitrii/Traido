"""Backtest strategy protocol + Stage 2 EMA trend stub (deterministic, no LLM)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from core.schemas import Bar
from quant.indicators import atr, ema, last, rsi
from quant.series import closes, highs, lows


@dataclass(frozen=True)
class EntrySignal:
    reasons: list[str]
    stop_distance_pct: float  # used only if ATR unavailable; prefer ATR stops in engine


@dataclass(frozen=True)
class ExitSignal:
    reasons: list[str]


class Strategy(Protocol):
    version: str

    def warm_up(self) -> int:
        """Minimum bars before first decision."""
        ...

    def evaluate_entry(self, bars: list[Bar]) -> EntrySignal | None:
        """bars = history through current bar inclusive. No lookahead beyond last bar."""
        ...

    def evaluate_exit(self, bars: list[Bar], entry_price: float) -> ExitSignal | None: ...


class EmaTrendStub:
    """
    Long-only stub for Stage 2 validation of the backtest harness.

    Entry: EMA50 > EMA200, close > EMA50, RSI between 40 and 65.
    Exit signal: close < EMA50 or RSI > 75.
    Stops/targets sized by engine via ATR.
    """

    version = "ema_trend_stub@0.1.0"

    def __init__(
        self,
        ema_fast: int = 50,
        ema_slow: int = 200,
        rsi_period: int = 14,
        rsi_min: float = 40.0,
        rsi_max: float = 65.0,
        rsi_exit: float = 75.0,
    ) -> None:
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.rsi_min = rsi_min
        self.rsi_max = rsi_max
        self.rsi_exit = rsi_exit

    def warm_up(self) -> int:
        return self.ema_slow + 5

    def evaluate_entry(self, bars: list[Bar]) -> EntrySignal | None:
        c = closes(bars)
        if len(c) < self.warm_up():
            return None
        e_fast = last(ema(c, self.ema_fast))
        e_slow = last(ema(c, self.ema_slow))
        r = last(rsi(c, self.rsi_period))
        if e_fast is None or e_slow is None or r is None:
            return None
        price = c[-1]
        if e_fast > e_slow and price > e_fast and self.rsi_min <= r <= self.rsi_max:
            return EntrySignal(
                reasons=[
                    "EMA50 above EMA200",
                    "Close above EMA50",
                    f"RSI {r:.1f} in entry band",
                ],
                stop_distance_pct=0.02,
            )
        return None

    def evaluate_exit(self, bars: list[Bar], entry_price: float) -> ExitSignal | None:
        del entry_price
        c = closes(bars)
        e_fast = last(ema(c, self.ema_fast))
        r = last(rsi(c, self.rsi_period))
        if e_fast is None or r is None:
            return None
        reasons: list[str] = []
        if c[-1] < e_fast:
            reasons.append("Close below EMA50")
        if r >= self.rsi_exit:
            reasons.append(f"RSI {r:.1f} overbought exit")
        if reasons:
            return ExitSignal(reasons=reasons)
        return None


def atr_stop_target(
    bars: list[Bar],
    entry: float,
    *,
    atr_period: int = 14,
    stop_mult: float = 1.5,
    target_rr: float = 2.0,
) -> tuple[float, float, float]:
    """Return (stop, target, atr_value) for a long entry at `entry`."""
    h, l, c = highs(bars), lows(bars), closes(bars)
    atr_v = last(atr(h, l, c, atr_period))
    if atr_v is None or atr_v <= 0:
        atr_v = entry * 0.02
    stop = entry - stop_mult * atr_v
    risk = entry - stop
    target = entry + target_rr * risk
    return stop, target, atr_v
