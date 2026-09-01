#!/usr/bin/env python3
"""Run Stage 2 backtest on fixture or Alpaca bars and persist journal rows."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

from core.config import get_settings
from core.enums import Timeframe
from database.repository import persist_backtest_summary
from market_data.factory import create_market_data_port
from quant.backtesting import BacktestEngine, EmaTrendStub


async def _load_bars(symbol: str, timeframe: Timeframe, start: datetime, end: datetime):
    settings = get_settings()
    # clear cached settings if .env loaded after import — recreate
    port = create_market_data_port(settings)
    return await port.get_bars(symbol, timeframe, start, end)


def main() -> None:
    parser = argparse.ArgumentParser(description="Traido Stage 2 backtest")
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--timeframe", default="1d", choices=["1d", "1h", "15m", "4h"])
    parser.add_argument("--start", default="2024-01-01")
    parser.add_argument("--end", default="2025-12-31")
    parser.add_argument("--equity", type=Decimal, default=Decimal(100000))
    parser.add_argument("--risk-pct", type=float, default=1.0)
    parser.add_argument("--persist", action="store_true", help="Write journal to DB")
    parser.add_argument(
        "--fixture",
        action="store_true",
        help="Force offline fixture bars (ignore Alpaca keys)",
    )
    args = parser.parse_args()

    tf = Timeframe(args.timeframe)
    start = datetime.fromisoformat(args.start).replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end).replace(tzinfo=UTC)

    if args.fixture:
        from market_data.providers.fixture import FixtureMarketData

        bars = asyncio.run(FixtureMarketData().get_bars(args.symbol, tf, start, end))
    else:
        bars = asyncio.run(_load_bars(args.symbol, tf, start, end))
    if not bars:
        raise SystemExit(f"No bars for {args.symbol} {tf} in range")

    engine = BacktestEngine(
        EmaTrendStub(),
        starting_equity=args.equity,
        risk_per_trade_pct=args.risk_pct,
    )
    summary = engine.run(args.symbol, tf, bars)

    print(f"strategy:     {summary.strategy_version}")
    print(f"symbol:       {summary.symbol} @ {summary.timeframe}")
    print(f"bars:         {len(bars)}")
    print(f"trades:       {summary.trade_count}")
    print(f"win_rate:     {summary.win_rate:.1%}")
    print(f"net_pnl:      {summary.net_pnl}")
    print(f"return:       {summary.return_pct:.2f}%")
    print(f"max_dd:       {summary.max_drawdown_pct:.2f}%")
    print(f"profit_factor:{summary.profit_factor}")
    print(f"avg_r:        {summary.avg_r}")
    print(f"ending_equity:{summary.ending_equity}")

    if args.persist:
        run_id = persist_backtest_summary(
            summary,
            params={"risk_pct": args.risk_pct, "bars": len(bars)},
            notes="Stage 2 CLI backtest",
        )
        print(f"persisted run_id: {run_id}")


if __name__ == "__main__":
    main()
