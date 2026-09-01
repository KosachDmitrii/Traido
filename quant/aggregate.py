"""OHLCV aggregation helpers (e.g. 1H → 4H)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from core.enums import Timeframe
from core.schemas import Bar


def aggregate_bars(bars: list[Bar], target: Timeframe, source_label: str) -> list[Bar]:
    """Aggregate sorted bars into a coarser timeframe. Currently supports 1h → 4h."""
    if target != Timeframe.H4:
        raise ValueError(f"unsupported aggregate target {target}")
    if not bars:
        return []

    buckets: dict[datetime, list[Bar]] = {}
    for bar in bars:
        ts = bar.ts
        # floor to 4h UTC bucket
        hour = (ts.hour // 4) * 4
        key = ts.replace(hour=hour, minute=0, second=0, microsecond=0)
        buckets.setdefault(key, []).append(bar)

    out: list[Bar] = []
    for key in sorted(buckets):
        group = buckets[key]
        out.append(
            Bar(
                symbol=group[0].symbol,
                timeframe=Timeframe.H4,
                ts=key,
                open=group[0].open,
                high=max(b.high for b in group),
                low=min(b.low for b in group),
                close=group[-1].close,
                volume=sum((b.volume for b in group), Decimal(0)),
                source=source_label,
            )
        )
    return out
