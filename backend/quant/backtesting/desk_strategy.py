"""Bar-based backtest adapter for the desk multi-TF strategy (Stage 8).

Approximates trader_desk rules on a single daily series so Evaluation / OOS
evidence attaches to the same StrategyVersion the desk stamps on paper/live
trades. Not a full replay of F3 WAIT timing — geometry and HTF filters only.
"""

from __future__ import annotations

from core.enums import Timeframe
from core.schemas import Bar
from quant.backtesting.strategy import EntrySignal, ExitSignal
from quant.engine import compute_features
from strategy.registry import LIVE_STRATEGY_KEY


class DeskConfluenceStrategy:
    """Long-only D1 approximation of trader_desk@1.2.0 structure+setup."""

    version = LIVE_STRATEGY_KEY

    def __init__(
        self,
        *,
        rsi_cap: float = 72.0,
        near_sma_frac: float = 0.025,
        chase_ext_frac: float = 0.04,
    ) -> None:
        self.rsi_cap = rsi_cap
        self.near_sma_frac = near_sma_frac
        self.chase_ext_frac = chase_ext_frac

    def warm_up(self) -> int:
        return 220

    def evaluate_entry(self, bars: list[Bar]) -> EntrySignal | None:
        if len(bars) < self.warm_up():
            return None
        snap = compute_features(bars[-1].symbol, Timeframe.D1, bars)
        ind = snap.indicators
        structure = snap.chart_patterns.get("structure")
        ema_ok = ind.get("ema50_above_ema200") is True
        if structure == "downtrend" or not ema_ok:
            return None
        if structure not in {"uptrend", "range"}:
            return None

        close = ind.get("close")
        sma20 = ind.get("sma_20")
        rsi_v = ind.get("rsi_14")
        if not isinstance(close, (int, float)) or not isinstance(sma20, (int, float)):
            return None
        if sma20 <= 0:
            return None
        if isinstance(rsi_v, (int, float)) and rsi_v >= self.rsi_cap:
            return None

        reasons = [f"structure={structure}", "ema50>ema200"]
        # Price-action shortcuts.
        if ind.get("pa_breakout") is True:
            reasons.append("breakout")
            return EntrySignal(reasons=reasons, stop_distance_pct=0.03)
        if ind.get("pa_retest") is True:
            reasons.append("retest")
            return EntrySignal(reasons=reasons, stop_distance_pct=0.025)
        if ind.get("pa_gap_up") is True and structure == "uptrend":
            reasons.append("gap_up")
            return EntrySignal(reasons=reasons, stop_distance_pct=0.03)

        dist = abs(float(close) - float(sma20)) / float(sma20)
        if float(close) > float(sma20) * (1.0 + self.chase_ext_frac):
            return None
        if dist > self.near_sma_frac * 2 and float(close) < float(sma20):
            return None
        reasons.append(f"pullback_sma20_{dist * 100:.1f}%")
        return EntrySignal(reasons=reasons, stop_distance_pct=0.025)

    def evaluate_exit(self, bars: list[Bar], entry_price: float) -> ExitSignal | None:
        if len(bars) < 30:
            return None
        snap = compute_features(bars[-1].symbol, Timeframe.D1, bars)
        structure = snap.chart_patterns.get("structure")
        ema_ok = snap.indicators.get("ema50_above_ema200") is True
        rsi_v = snap.indicators.get("rsi_14")
        if structure == "downtrend" or ema_ok is False:
            return ExitSignal(reasons=["htf_broke"])
        if isinstance(rsi_v, (int, float)) and rsi_v >= 78:
            return ExitSignal(reasons=["rsi_exhaustion"])
        close = snap.indicators.get("close")
        if isinstance(close, (int, float)) and entry_price > 0:
            if float(close) <= entry_price * 0.92:
                return ExitSignal(reasons=["soft_stop_proxy"])
        return None
