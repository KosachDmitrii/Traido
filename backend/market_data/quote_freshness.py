"""Align quote timestamps with fresher last-trade prints (IEX book lag)."""

from __future__ import annotations

from datetime import UTC, datetime

from core.schemas import Quote


def quote_with_trade_freshness(
    quote: Quote | None,
    *,
    trade_ts: datetime | None,
) -> Quote | None:
    """When the tape moved after the IEX book timestamp, use the trade clock."""
    if quote is None or trade_ts is None:
        return quote
    q_ts = quote.ts
    if q_ts.tzinfo is None:
        return quote
    trade = trade_ts.astimezone(UTC)
    if trade > q_ts.astimezone(UTC):
        return quote.model_copy(update={"ts": trade})
    return quote
