"""
Bar-by-bar backtest engine.

Rules:
- No lookahead: decisions use bars[:i+1] only.
- Long-only in Stage 2.
- Protective stop and target checked on each bar using high/low (intrabar).
- Stop takes priority over target if both touched in the same bar (conservative).
- Every fill is priced through `quant.costs.CostModel`: spread and slippage move
  the price against us and commissions/regulatory fees are deducted. Reported
  `net_pnl` is therefore after all modelled friction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from core.enums import ExitReason, Timeframe
from core.schemas import BacktestSummary, BacktestTrade, Bar
from quant.backtesting.metrics import (
    average_loss_usd,
    average_r,
    average_win_usd,
    cagr_pct,
    calmar_ratio,
    expectancy_usd,
    largest_loss_usd,
    max_consecutive_losses,
    max_drawdown_pct,
    profit_factor,
    sharpe_ratio,
    sortino_ratio,
    win_rate,
)
from quant.backtesting.strategy import Strategy, atr_stop_target
from quant.costs import DEFAULT_COST_MODEL, CostModel, FillKind

_CENT = Decimal("0.01")


@dataclass
class _OpenTrade:
    symbol: str
    entry: Decimal
    entry_reference: Decimal
    stop: Decimal
    target: Decimal
    qty: Decimal
    entry_reasons: list[str]
    indicators_at_entry: dict[str, Any]
    opened_at: object
    entry_index: int
    risk_reward_planned: float
    entry_fees: Decimal
    mfe: Decimal = field(default_factory=lambda: Decimal(0))
    mae: Decimal = field(default_factory=lambda: Decimal(0))


class BacktestEngine:
    def __init__(
        self,
        strategy: Strategy,
        *,
        starting_equity: Decimal = Decimal(100000),
        risk_per_trade_pct: float = 1.0,
        atr_stop_mult: float = 1.5,
        target_rr: float = 2.0,
        max_positions: int = 1,
        costs: CostModel | None = None,
    ) -> None:
        if risk_per_trade_pct <= 0 or risk_per_trade_pct > 5:
            raise ValueError("risk_per_trade_pct must be in (0, 5]")
        self.strategy = strategy
        self.starting_equity = starting_equity
        self.risk_per_trade_pct = risk_per_trade_pct
        self.atr_stop_mult = atr_stop_mult
        self.target_rr = target_rr
        self.max_positions = max_positions
        self.costs = costs if costs is not None else DEFAULT_COST_MODEL

    def run(
        self,
        symbol: str,
        timeframe: Timeframe,
        bars: list[Bar],
    ) -> BacktestSummary:
        if len(bars) < self.strategy.warm_up():
            raise ValueError("not enough bars for strategy warm-up")

        equity = self.starting_equity
        cash = self.starting_equity
        open_trade: _OpenTrade | None = None
        closed: list[BacktestTrade] = []
        equity_curve: list[float] = [float(equity)]
        warm = self.strategy.warm_up()

        for i in range(warm, len(bars)):
            window = bars[: i + 1]
            bar = bars[i]

            if open_trade is not None:
                # Update MFE / MAE using intrabar extremes
                up = Decimal(str(bar.high)) - open_trade.entry
                down = open_trade.entry - Decimal(str(bar.low))
                open_trade.mfe = max(open_trade.mfe, up)
                open_trade.mae = max(open_trade.mae, down)

                exit_reference: Decimal | None = None
                exit_kind = FillKind.MARKET
                exit_reasons: list[str] = []

                # Conservative: stop before target if both hit
                if Decimal(str(bar.low)) <= open_trade.stop:
                    exit_reference = open_trade.stop
                    exit_kind = FillKind.STOP
                    exit_reasons = [ExitReason.STOP.value]
                elif Decimal(str(bar.high)) >= open_trade.target:
                    exit_reference = open_trade.target
                    exit_kind = FillKind.LIMIT
                    exit_reasons = [ExitReason.TARGET.value]
                else:
                    sig = self.strategy.evaluate_exit(window, float(open_trade.entry))
                    if sig is not None:
                        exit_reference = Decimal(str(bar.close))
                        exit_kind = FillKind.MARKET
                        exit_reasons = [ExitReason.SIGNAL.value, *sig.reasons]

                if exit_reference is not None:
                    trade, proceeds = self._close(
                        open_trade,
                        exit_reference,
                        exit_kind,
                        exit_reasons,
                        bar.ts,
                        i,
                    )
                    closed.append(trade)
                    cash += proceeds
                    equity = cash
                    open_trade = None

            if open_trade is None:
                signal = self.strategy.evaluate_entry(window)
                if signal is not None:
                    reference = Decimal(str(bar.close))
                    entry = self.costs.fill_price(reference, side="buy", kind=FillKind.MARKET)
                    stop_f, target_f, _atr = atr_stop_target(
                        window,
                        float(entry),
                        stop_mult=self.atr_stop_mult,
                        target_rr=self.target_rr,
                    )
                    stop = Decimal(str(round(stop_f, 4)))
                    target = Decimal(str(round(target_f, 4)))
                    risk_per_share = entry - stop
                    if risk_per_share <= 0:
                        equity_curve.append(float(equity))
                        continue
                    risk_budget = equity * Decimal(str(self.risk_per_trade_pct / 100.0))
                    qty = (risk_budget / risk_per_share).quantize(Decimal("0.0001"))
                    if qty <= 0:
                        equity_curve.append(float(equity))
                        continue
                    cost = qty * entry
                    fees = self.costs.fees(qty, entry, side="buy")
                    if cost + fees > cash:
                        qty = (cash / entry * Decimal("0.999")).quantize(Decimal("0.0001"))
                        cost = qty * entry
                        fees = self.costs.fees(qty, entry, side="buy")
                    if qty <= 0 or cost + fees > cash:
                        equity_curve.append(float(equity))
                        continue
                    cash -= cost + fees
                    open_trade = _OpenTrade(
                        symbol=symbol.upper(),
                        entry=entry,
                        entry_reference=reference,
                        stop=stop,
                        target=target,
                        qty=qty,
                        entry_reasons=list(signal.reasons),
                        indicators_at_entry={
                            "close": float(reference),
                            "fill": float(entry),
                            "atr_stop": float(stop),
                            "atr_target": float(target),
                        },
                        opened_at=bar.ts,
                        entry_index=i,
                        risk_reward_planned=self.target_rr,
                        entry_fees=fees,
                    )

            # Mark-to-market equity
            mtm = cash
            if open_trade is not None:
                mtm += open_trade.qty * Decimal(str(bar.close))
            equity = mtm
            equity_curve.append(float(equity))

        # Force flat at end of series
        if open_trade is not None:
            last = bars[-1]
            trade, proceeds = self._close(
                open_trade,
                Decimal(str(last.close)),
                FillKind.MARKET,
                [ExitReason.END_OF_DATA.value],
                last.ts,
                len(bars) - 1,
            )
            closed.append(trade)
            cash += proceeds
            equity = cash
            equity_curve.append(float(equity))

        wins = sum(1 for t in closed if t.pnl > 0)
        losses = sum(1 for t in closed if t.pnl <= 0)
        net = equity - self.starting_equity
        avg_bars = sum(t.bars_held for t in closed) / len(closed) if closed else None
        pf = profit_factor(closed)
        pf_out = 999.0 if pf == float("inf") else pf

        total_costs = sum((t.costs for t in closed), Decimal(0))

        return BacktestSummary(
            strategy_version=self.strategy.version,
            symbol=symbol.upper(),
            timeframe=timeframe,
            starting_equity=self.starting_equity,
            ending_equity=equity.quantize(_CENT),
            net_pnl=net.quantize(_CENT),
            return_pct=float(net / self.starting_equity * 100),
            trade_count=len(closed),
            win_count=wins,
            loss_count=losses,
            win_rate=win_rate(closed),
            profit_factor=pf_out,
            max_drawdown_pct=max_drawdown_pct(equity_curve),
            avg_r=average_r(closed),
            avg_bars_held=avg_bars,
            trades=closed,
            gross_pnl=(net + total_costs).quantize(_CENT),
            total_costs=total_costs.quantize(_CENT),
            sharpe=sharpe_ratio(equity_curve, timeframe),
            sortino=sortino_ratio(equity_curve, timeframe),
            calmar=calmar_ratio(equity_curve, timeframe),
            cagr_pct=cagr_pct(equity_curve, timeframe),
            expectancy_usd=expectancy_usd(closed),
            expectancy_r=average_r(closed),
            avg_win_usd=average_win_usd(closed),
            avg_loss_usd=average_loss_usd(closed),
            largest_loss_usd=largest_loss_usd(closed),
            max_consecutive_losses=max_consecutive_losses(closed),
            equity_curve=equity_curve,
        )

    def _close(
        self,
        open_trade: _OpenTrade,
        exit_reference: Decimal,
        exit_kind: FillKind,
        exit_reasons: list[str],
        closed_at: object,
        exit_index: int,
    ) -> tuple[BacktestTrade, Decimal]:
        exit_price = self.costs.fill_price(exit_reference, side="sell", kind=exit_kind)
        exit_fees = self.costs.fees(open_trade.qty, exit_price, side="sell")

        proceeds = open_trade.qty * exit_price - exit_fees
        pnl = (exit_price - open_trade.entry) * open_trade.qty - open_trade.entry_fees - exit_fees
        pnl_pct = float((exit_price - open_trade.entry) / open_trade.entry * 100)

        entry_slip = (open_trade.entry - open_trade.entry_reference) * open_trade.qty
        exit_slip = (exit_reference - exit_price) * open_trade.qty
        costs = entry_slip + exit_slip + open_trade.entry_fees + exit_fees

        mfe_pct = float(open_trade.mfe / open_trade.entry * 100) if open_trade.entry else 0.0
        mae_pct = float(open_trade.mae / open_trade.entry * 100) if open_trade.entry else 0.0

        trade = BacktestTrade(
            symbol=open_trade.symbol,
            entry=open_trade.entry,
            exit=exit_price,
            stop=open_trade.stop,
            target=open_trade.target,
            qty=open_trade.qty,
            pnl=pnl.quantize(_CENT),
            pnl_pct=pnl_pct,
            mfe=open_trade.mfe.quantize(Decimal("0.0001")),
            mae=open_trade.mae.quantize(Decimal("0.0001")),
            mfe_pct=mfe_pct,
            mae_pct=mae_pct,
            entry_reasons=open_trade.entry_reasons,
            exit_reasons=exit_reasons,
            strategy_version=self.strategy.version,
            indicators_at_entry=open_trade.indicators_at_entry,
            opened_at=open_trade.opened_at,  # type: ignore[arg-type]
            closed_at=closed_at,  # type: ignore[arg-type]
            bars_held=max(1, exit_index - open_trade.entry_index),
            risk_reward_planned=open_trade.risk_reward_planned,
            costs=costs.quantize(_CENT),
        )
        return trade, proceeds
