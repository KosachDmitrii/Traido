"""Counterfactual OHLC path evaluation. Not broker fills or a profitability claim."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from core.schemas import Bar


def evaluate_forward_path(
    *,
    decided_at: datetime,
    entry: Decimal,
    stop: Decimal,
    target: Decimal,
    bars: list[Bar],
) -> dict[str, object]:
    """Only later bars; no assumed order inside a bar touching both barriers.

    Entry is hypothetical at the specified price only when a later bar spans it.
    No PnL is reported: spread, slippage, queue and actual fills are unknown.
    """
    if not stop < entry < target:
        raise ValueError("INVALID_GEOMETRY")
    future = sorted((bar for bar in bars if bar.ts > decided_at), key=lambda b: b.ts)
    entered = False
    mfe_r = Decimal(0)
    mae_r = Decimal(0)
    for bar in future:
        if not entered:
            if not bar.low <= entry <= bar.high:
                continue
            entered = True
            # Intrabar extrema may precede entry. Do not claim them as returns.
            if bar.low <= stop or bar.high >= target:
                return {"status": "ambiguous_entry_bar", "bars": len(future)}
            continue
        if bar.open <= stop:
            return {
                "status": "stop_gap",
                "hypothetical_r": float((bar.open - entry) / (entry - stop)),
            }
        if bar.low <= stop and bar.high >= target:
            return {"status": "ambiguous_bar", "bars": len(future)}
        if bar.low <= stop:
            return {"status": "stop_first", "hypothetical_r": -1.0}
        if bar.high >= target:
            return {
                "status": "target_first",
                "hypothetical_r": float((target - entry) / (entry - stop)),
            }
        mfe_r = max(mfe_r, (bar.high - entry) / (entry - stop))
        mae_r = min(mae_r, (bar.low - entry) / (entry - stop))
    return {
        "status": "open" if entered else "not_entered" if future else "no_forward_data",
        "bars": len(future),
        "mfe_r": float(mfe_r),
        "mae_r": float(mae_r),
    }
