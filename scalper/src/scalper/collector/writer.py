"""Buffered parquet writer.

Layout: <data_dir>/<kind>/<SYMBOL>/<YYYY-MM-DD>/part-<utc stamp>-<n>.parquet
The date is the exchange's (America/New_York) date of the event, not UTC's.
Each flush writes a new part file, so nothing is ever appended to or rewritten.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

NY = ZoneInfo("America/New_York")

SCHEMAS: dict[str, pa.Schema] = {
    "trades": pa.schema([
        ("ts", pa.timestamp("ns", tz="UTC")),
        ("recv_ts", pa.timestamp("ns", tz="UTC")),
        ("symbol", pa.string()),
        ("price", pa.float64()),
        ("size", pa.float64()),
        ("exchange", pa.string()),
        ("conditions", pa.string()),
        ("trade_id", pa.int64()),
        ("tape", pa.string()),
    ]),
    "quotes": pa.schema([
        ("ts", pa.timestamp("ns", tz="UTC")),
        ("recv_ts", pa.timestamp("ns", tz="UTC")),
        ("symbol", pa.string()),
        ("bid_price", pa.float64()),
        ("bid_size", pa.float64()),
        ("bid_exchange", pa.string()),
        ("ask_price", pa.float64()),
        ("ask_size", pa.float64()),
        ("ask_exchange", pa.string()),
        ("conditions", pa.string()),
        ("tape", pa.string()),
    ]),
}


def market_date(ts: datetime) -> str:
    return ts.astimezone(NY).date().isoformat()


def is_rth(ts: datetime) -> bool:
    local = ts.astimezone(NY)
    if local.weekday() >= 5:
        return False
    minutes = local.hour * 60 + local.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


class ParquetWriter:
    def __init__(self, data_dir: Path, flush_rows: int, flush_seconds: float) -> None:
        self._dir = Path(data_dir)
        self._flush_rows = flush_rows
        self._flush_seconds = flush_seconds
        self._lock = threading.Lock()
        self._buffers: dict[tuple[str, str, str], list[dict]] = {}
        self._opened: dict[tuple[str, str, str], float] = {}
        self._seq = 0
        self.rows_written = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._timer, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def add(self, kind: str, row: dict) -> None:
        key = (kind, row["symbol"], market_date(row["ts"]))
        with self._lock:
            buf = self._buffers.setdefault(key, [])
            self._opened.setdefault(key, time.monotonic())
            buf.append(row)
            if len(buf) >= self._flush_rows:
                self._flush_key(key)

    def flush_all(self) -> None:
        with self._lock:
            for key in list(self._buffers):
                self._flush_key(key)

    def close(self) -> None:
        self._stop.set()
        self.flush_all()

    def _timer(self) -> None:
        while not self._stop.wait(1.0):
            now = time.monotonic()
            with self._lock:
                for key in list(self._buffers):
                    if now - self._opened.get(key, now) >= self._flush_seconds:
                        self._flush_key(key)

    def _flush_key(self, key: tuple[str, str, str]) -> None:
        rows = self._buffers.pop(key, [])
        self._opened.pop(key, None)
        if not rows:
            return
        kind, symbol, day = key
        schema = SCHEMAS[kind]
        table = pa.Table.from_pylist(rows, schema=schema)
        out_dir = self._dir / kind / symbol / day
        out_dir.mkdir(parents=True, exist_ok=True)
        self._seq += 1
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = out_dir / f"part-{stamp}-{self._seq:06d}.parquet"
        tmp = path.with_suffix(".tmp")
        pq.write_table(table, tmp, compression="zstd")
        tmp.replace(path)
        self.rows_written += len(rows)
