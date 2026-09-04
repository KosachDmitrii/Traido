"""Immutable executable geometry — one authority for admission, evidence, intent."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from core.schemas import Quote
from trading.effective_rr import compute_effective_rr
from trading.geometry_hash import compute_geometry_hash


@dataclass(frozen=True)
class ExecutionGeometry:
    entry: Decimal
    stop: Decimal
    target: Decimal
    quote_bid: Decimal
    quote_ask: Decimal
    quote_ts: datetime
    quote_source: str | None
    geometry_hash: str
    effective_rr: float | None


def resolve_capital_atr(
    *,
    facts_atr: float | None = None,
    snapshot_atr: float | None = None,
    indicator_atr: float | None = None,
) -> float | None:
    """Return real ATR only — never synthesize for capital path."""
    for candidate in (facts_atr, snapshot_atr, indicator_atr):
        if isinstance(candidate, (int, float)) and candidate > 0:
            return float(candidate)
    return None


def build_execution_geometry(
    *,
    entry: Decimal | float,
    stop: Decimal | float,
    target: Decimal | float,
    quote: Quote,
    exec_timeframe: str,
    strategy_version: str,
    zone_low: float | None = None,
    zone_high: float | None = None,
    atr: float | None = None,
) -> ExecutionGeometry:
    """Build one geometry bundle used across admission, evidence, and intent."""
    ent = Decimal(str(entry))
    stp = Decimal(str(stop))
    tgt = Decimal(str(target))
    bid = quote.bid if quote.bid is not None else Decimal(0)
    ask = quote.ask if quote.ask is not None else ent
    rr = compute_effective_rr(
        entry=ent,
        stop=stp,
        target=tgt,
        quote=quote,
        zone_low=zone_low,
        zone_high=zone_high,
        atr=atr,
    )
    gh = compute_geometry_hash(
        entry=float(ent),
        stop=float(stp),
        target=float(tgt),
        exec_timeframe=exec_timeframe,
        strategy_version=strategy_version,
    )
    return ExecutionGeometry(
        entry=ent,
        stop=stp,
        target=tgt,
        quote_bid=bid,
        quote_ask=ask,
        quote_ts=quote.ts,
        quote_source=getattr(quote, "source", None),
        geometry_hash=gh,
        effective_rr=rr.effective_rr,
    )
