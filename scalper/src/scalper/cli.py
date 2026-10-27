"""scalper check | scalper collect"""
from __future__ import annotations

import argparse
import logging

from .config import credentials, load_config


def _check(args) -> int:
    from alpaca.common.exceptions import APIError
    from alpaca.data.enums import DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestQuoteRequest

    cfg = load_config(args.config)
    key, secret = credentials()
    client = StockHistoricalDataClient(key, secret)
    feed = DataFeed.SIP if cfg.feed == "sip" else DataFeed.IEX
    symbol = cfg.symbols[0]
    try:
        quotes = client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=feed))
    except APIError as exc:
        print(f"FAIL feed={cfg.feed}: HTTP {getattr(exc, 'status_code', '?')}")
        print("The key is wrong, or the market-data plan does not include this feed.")
        return 1
    q = quotes[symbol]
    print(f"OK feed={cfg.feed} {symbol} bid={q.bid_price} ask={q.ask_price} at {q.timestamp}")
    return 0


def _collect(args) -> int:
    from .collector.stream import Collector

    cfg = load_config(args.config)
    key, secret = credentials()
    Collector(cfg, key, secret).run()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="scalper")
    parser.add_argument("--config", default="config/collector.toml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="verify credentials and feed access").set_defaults(fn=_check)
    sub.add_parser("collect", help="record live trades/quotes to parquet").set_defaults(fn=_collect)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    raise SystemExit(args.fn(args))
