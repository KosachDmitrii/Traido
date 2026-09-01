"""Aggregate performance metrics for a finished backtest."""

from __future__ import annotations

import math
from decimal import Decimal
from itertools import pairwise

from core.enums import Timeframe
from core.schemas import BacktestTrade

PERIODS_PER_YEAR: dict[Timeframe, float] = {
    Timeframe.D1: 252.0,
    Timeframe.H4: 252.0 * 2,
    Timeframe.H1: 252.0 * 6.5,
    Timeframe.M15: 252.0 * 26,
    Timeframe.M5: 252.0 * 78,
}


def win_rate(trades: list[BacktestTrade]) -> float:
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if t.pnl > 0)
    return wins / len(trades)


def profit_factor(trades: list[BacktestTrade]) -> float | None:
    gains = sum((t.pnl for t in trades if t.pnl > 0), Decimal(0))
    losses = sum((-t.pnl for t in trades if t.pnl < 0), Decimal(0))
    if losses == 0:
        return None if gains == 0 else float("inf")
    return float(gains / losses)


def max_drawdown_pct(equity_curve: list[float]) -> float:
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for eq in equity_curve:
        peak = max(peak, eq)
        if peak <= 0:
            continue
        dd = (peak - eq) / peak
        max_dd = max(max_dd, dd)
    return max_dd * 100.0


def average_r(trades: list[BacktestTrade]) -> float | None:
    rs = _r_multiples(trades)
    if not rs:
        return None
    return sum(rs) / len(rs)


def _r_multiples(trades: list[BacktestTrade]) -> list[float]:
    rs: list[float] = []
    for t in trades:
        risk = float(t.entry - t.stop)
        if risk <= 0:
            continue
        rs.append(float(t.pnl) / (risk * float(t.qty)))
    return rs


def period_returns(equity_curve: list[float]) -> list[float]:
    """Simple period-over-period returns from a mark-to-market equity curve."""
    out: list[float] = []
    for prev, cur in pairwise(equity_curve):
        if prev <= 0:
            continue
        out.append((cur - prev) / prev)
    return out


def _stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


def sharpe_ratio(
    equity_curve: list[float],
    timeframe: Timeframe,
    *,
    risk_free_annual: float = 0.0,
) -> float | None:
    """Annualised Sharpe from the equity curve. None when there is no variance to measure."""
    rets = period_returns(equity_curve)
    if len(rets) < 2:
        return None
    ppy = PERIODS_PER_YEAR.get(timeframe, 252.0)
    rf_per_period = risk_free_annual / ppy
    excess = [r - rf_per_period for r in rets]
    sd = _stdev(excess)
    if sd == 0:
        return None
    mean = sum(excess) / len(excess)
    return (mean / sd) * math.sqrt(ppy)


def sortino_ratio(
    equity_curve: list[float],
    timeframe: Timeframe,
    *,
    risk_free_annual: float = 0.0,
) -> float | None:
    """Like Sharpe but only downside deviation is treated as risk."""
    rets = period_returns(equity_curve)
    if len(rets) < 2:
        return None
    ppy = PERIODS_PER_YEAR.get(timeframe, 252.0)
    rf_per_period = risk_free_annual / ppy
    excess = [r - rf_per_period for r in rets]
    downside = [r for r in excess if r < 0]
    if not downside:
        return None
    dd = math.sqrt(sum(r**2 for r in downside) / len(excess))
    if dd == 0:
        return None
    mean = sum(excess) / len(excess)
    return (mean / dd) * math.sqrt(ppy)


def cagr_pct(equity_curve: list[float], timeframe: Timeframe) -> float | None:
    """Compound annual growth rate implied by the curve length and timeframe."""
    if len(equity_curve) < 2:
        return None
    start, end = equity_curve[0], equity_curve[-1]
    if start <= 0 or end <= 0:
        return None
    ppy = PERIODS_PER_YEAR.get(timeframe, 252.0)
    years = (len(equity_curve) - 1) / ppy
    if years <= 0:
        return None
    return float(((end / start) ** (1 / years) - 1) * 100.0)


def calmar_ratio(equity_curve: list[float], timeframe: Timeframe) -> float | None:
    """CAGR per unit of max drawdown — the ratio that matters for sizing up."""
    cagr = cagr_pct(equity_curve, timeframe)
    mdd = max_drawdown_pct(equity_curve)
    if cagr is None or mdd <= 0:
        return None
    return cagr / mdd


def expectancy_usd(trades: list[BacktestTrade]) -> float | None:
    if not trades:
        return None
    return float(sum((t.pnl for t in trades), Decimal(0))) / len(trades)


def expectancy_r(trades: list[BacktestTrade]) -> float | None:
    return average_r(trades)


def average_win_usd(trades: list[BacktestTrade]) -> float | None:
    wins = [float(t.pnl) for t in trades if t.pnl > 0]
    return sum(wins) / len(wins) if wins else None


def average_loss_usd(trades: list[BacktestTrade]) -> float | None:
    losses = [float(t.pnl) for t in trades if t.pnl < 0]
    return sum(losses) / len(losses) if losses else None


def largest_loss_usd(trades: list[BacktestTrade]) -> float | None:
    losses = [float(t.pnl) for t in trades if t.pnl < 0]
    return min(losses) if losses else None


def max_consecutive_losses(trades: list[BacktestTrade]) -> int:
    worst = 0
    run = 0
    for t in trades:
        if t.pnl <= 0:
            run += 1
            worst = max(worst, run)
        else:
            run = 0
    return worst


def total_costs_usd(trades: list[BacktestTrade]) -> float:
    return float(sum((t.costs for t in trades), Decimal(0)))


def gross_pnl_usd(trades: list[BacktestTrade]) -> float:
    """Net P&L plus the friction that was charged — shows what costs consumed."""
    return float(sum((t.pnl + t.costs for t in trades), Decimal(0)))
