"""Position sizing — deterministic."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal


def size_long_shares(
    *,
    equity: Decimal,
    entry: Decimal,
    stop: Decimal,
    risk_pct: float,
    max_position_pct: float,
    cash: Decimal,
) -> tuple[Decimal, Decimal]:
    """
    Return (qty, max_loss_usd) for a long.
    Qty is limited by risk budget, max position %, and available cash.
    """
    risk_per_share = entry - stop
    if risk_per_share <= 0 or entry <= 0 or equity <= 0:
        return Decimal(0), Decimal(0)

    risk_budget = equity * Decimal(str(risk_pct / 100.0))
    qty_by_risk = (risk_budget / risk_per_share).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)

    max_notional = equity * Decimal(str(max_position_pct / 100.0))
    qty_by_pos = (max_notional / entry).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    qty_by_cash = (cash / entry).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)

    qty = min(qty_by_risk, qty_by_pos, qty_by_cash)
    if qty < Decimal("0.0001"):
        return Decimal(0), Decimal(0)

    max_loss = (qty * risk_per_share).quantize(Decimal("0.01"))
    return qty, max_loss
