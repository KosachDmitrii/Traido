from datetime import datetime, timezone

import pyarrow.parquet as pq

from scalper.collector.writer import ParquetWriter, is_rth, market_date


def _trade(ts, symbol="SPY"):
    return {"ts": ts, "recv_ts": ts, "symbol": symbol, "price": 500.0, "size": 100.0,
            "exchange": "V", "conditions": "@", "trade_id": 1, "tape": "B"}


def test_market_date_is_exchange_date():
    # 01:00 UTC on the 2nd is still the 1st in New York
    assert market_date(datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)) == "2026-10-01"


def test_rth_bounds():
    assert is_rth(datetime(2026, 10, 27, 13, 30, tzinfo=timezone.utc))      # 09:30 ET
    assert not is_rth(datetime(2026, 10, 27, 13, 29, tzinfo=timezone.utc))  # 09:29 ET
    assert not is_rth(datetime(2026, 10, 27, 20, 0, tzinfo=timezone.utc))   # 16:00 ET
    assert not is_rth(datetime(2026, 10, 24, 15, 0, tzinfo=timezone.utc))   # Saturday


def test_flush_on_row_count_and_close(tmp_path):
    w = ParquetWriter(tmp_path, flush_rows=2, flush_seconds=999)
    ts = datetime(2026, 10, 27, 15, 0, tzinfo=timezone.utc)
    w.add("trades", _trade(ts))
    w.add("trades", _trade(ts))  # hits flush_rows
    w.add("trades", _trade(ts, "QQQ"))
    w.close()
    spy = list((tmp_path / "trades" / "SPY" / "2026-10-27").glob("*.parquet"))
    qqq = list((tmp_path / "trades" / "QQQ" / "2026-10-27").glob("*.parquet"))
    assert len(spy) == 1 and pq.read_table(spy[0]).num_rows == 2
    assert len(qqq) == 1 and pq.read_table(qqq[0]).num_rows == 1
    assert w.rows_written == 3
