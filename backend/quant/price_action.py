"""Price-action detectors — breakout, retest, gap (Stage 8).

Environment-agnostic: same signals for paper and live. Deterministic heuristics
only; no LLM.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PriceActionReading:
    breakout: bool
    retest: bool
    gap_up: bool
    gap_down: bool
    gap_pct: float | None
    resistance: float | None
    support: float | None
    reasons: tuple[str, ...]

    def as_flags(self) -> dict[str, bool | float | None]:
        return {
            "breakout": self.breakout,
            "retest": self.retest,
            "gap_up": self.gap_up,
            "gap_down": self.gap_down,
            "gap_pct": self.gap_pct,
            "resistance": self.resistance,
            "support": self.support,
        }


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


def detect_price_action(
    opens: list[float],
    highs: list[float],
    lows: list[float],
    closes: list[float],
    *,
    lookback: int = 20,
    breakout_buffer: float = 0.001,
    retest_tol: float = 0.008,
    gap_min_pct: float = 0.015,
) -> PriceActionReading:
    """Read the last bar against recent swing structure and the prior close."""
    reasons: list[str] = []
    n = len(closes)
    if n < max(10, lookback + 2):
        return PriceActionReading(
            False, False, False, False, None, None, None, ("insufficient_bars",)
        )

    window_h = highs[-(lookback + 1) : -1]
    window_l = lows[-(lookback + 1) : -1]
    resistance = max(window_h) if window_h else None
    support = min(window_l) if window_l else None
    close = closes[-1]
    prev_close = closes[-2]
    open_ = opens[-1]

    breakout = False
    if resistance is not None and close > resistance * (1.0 + breakout_buffer):
        breakout = True
        reasons.append(f"breakout_above_{resistance:.4g}")

    retest = False
    sh = _swing_highs(highs[:-1])
    if sh and resistance is not None:
        # Pullback that tags prior breakout level from above and holds.
        near = abs(close - resistance) / resistance <= retest_tol
        held = close >= resistance * (1.0 - retest_tol) and lows[-1] >= resistance * (
            1.0 - retest_tol * 2
        )
        prior_broke = any(closes[i] > resistance for i in range(max(0, n - lookback), n - 1))
        if near and held and prior_broke and close >= open_:
            retest = True
            reasons.append("retest_hold")

    gap_pct = (open_ - prev_close) / prev_close if prev_close else None
    gap_up = bool(gap_pct is not None and gap_pct >= gap_min_pct)
    gap_down = bool(gap_pct is not None and gap_pct <= -gap_min_pct)
    if gap_up and gap_pct is not None:
        reasons.append(f"gap_up_{gap_pct * 100:.1f}%")
    if gap_down and gap_pct is not None:
        reasons.append(f"gap_down_{gap_pct * 100:.1f}%")

    if support is not None:
        reasons.append(f"support_{support:.4g}")

    return PriceActionReading(
        breakout=breakout,
        retest=retest,
        gap_up=gap_up,
        gap_down=gap_down,
        gap_pct=gap_pct,
        resistance=resistance,
        support=support,
        reasons=tuple(reasons) if reasons else ("no_price_action",),
    )
