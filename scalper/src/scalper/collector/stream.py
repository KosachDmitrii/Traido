"""Live trades + quotes from Alpaca into the parquet writer.

Credentials are passed to the SDK and never logged.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream

from ..config import CollectorConfig
from .writer import ParquetWriter, is_rth

log = logging.getLogger("scalper.collector")


def _conditions(value) -> str:
    return ",".join(value) if value else ""


class Collector:
    def __init__(self, cfg: CollectorConfig, key: str, secret: str) -> None:
        self._cfg = cfg
        self._key = key
        self._secret = secret
        self._writer = ParquetWriter(cfg.data_dir, cfg.flush_rows, cfg.flush_seconds)
        self.seen = {"trades": 0, "quotes": 0}

    async def _on_trade(self, t) -> None:
        if self._cfg.rth_only and not is_rth(t.timestamp):
            return
        self.seen["trades"] += 1
        self._writer.add("trades", {
            "ts": t.timestamp,
            "recv_ts": datetime.now(timezone.utc),
            "symbol": t.symbol,
            "price": float(t.price),
            "size": float(t.size),
            "exchange": str(t.exchange or ""),
            "conditions": _conditions(t.conditions),
            "trade_id": int(t.id) if t.id is not None else None,
            "tape": str(t.tape or ""),
        })

    async def _on_quote(self, q) -> None:
        if self._cfg.rth_only and not is_rth(q.timestamp):
            return
        self.seen["quotes"] += 1
        self._writer.add("quotes", {
            "ts": q.timestamp,
            "recv_ts": datetime.now(timezone.utc),
            "symbol": q.symbol,
            "bid_price": float(q.bid_price),
            "bid_size": float(q.bid_size),
            "bid_exchange": str(q.bid_exchange or ""),
            "ask_price": float(q.ask_price),
            "ask_size": float(q.ask_size),
            "ask_exchange": str(q.ask_exchange or ""),
            "conditions": _conditions(q.conditions),
            "tape": str(q.tape or ""),
        })

    def run(self) -> None:
        """Block until interrupted. Reconnects with capped backoff if the stream dies."""
        self._writer.start()
        feed = DataFeed.SIP if self._cfg.feed == "sip" else DataFeed.IEX
        delay = 2.0
        try:
            while True:
                stream = StockDataStream(self._key, self._secret, feed=feed)
                if self._cfg.trades:
                    stream.subscribe_trades(self._on_trade, *self._cfg.symbols)
                if self._cfg.quotes:
                    stream.subscribe_quotes(self._on_quote, *self._cfg.symbols)
                started = time.monotonic()
                try:
                    log.info("connecting feed=%s symbols=%s", self._cfg.feed, ",".join(self._cfg.symbols))
                    stream.run()
                    return  # clean stop
                except KeyboardInterrupt:
                    raise
                except Exception as exc:  # network drop, 406 connection limit, ...
                    log.error("stream died: %s", type(exc).__name__)
                if time.monotonic() - started > 60:
                    delay = 2.0
                log.info("reconnecting in %.0fs", delay)
                time.sleep(delay)
                delay = min(delay * 2, 60.0)
        except KeyboardInterrupt:
            log.info("stopping")
        finally:
            self._writer.close()
            log.info("wrote %d rows (seen %s)", self._writer.rows_written, self.seen)
