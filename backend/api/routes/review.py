"""Stage 5 — positions + review analytics."""

from __future__ import annotations

from fastapi import APIRouter

from agents.review.agent import build_review
from broker.factory import create_broker
from core.config import get_settings
from market_data.providers.company_name import attach_company_names
from trading.desk_positions import protective_stop_for_display
from trading.ledger import LEDGER
from trading.pricing import round_equity_price

router = APIRouter(prefix="/api/v1", tags=["review"])


@router.get("/review")
async def review(live_only: bool = True) -> dict:
    """Journal analytics — Review Agent (no trading authority)."""
    payload = build_review(live_only=live_only).to_dict()
    await attach_company_names(payload.get("recent") or [], get_settings().finnhub_api_key)
    return payload


@router.get("/positions")
async def positions() -> dict:
    """Open broker positions + Traido ledger metadata."""
    settings = get_settings()
    broker = create_broker(settings)
    broker_pos = await broker.list_positions()
    try:
        open_orders = await broker.list_open_orders()
    except Exception:  # noqa: BLE001
        open_orders = []
    ledger = LEDGER.get_open()
    by_sym = {r.symbol: r for r in ledger}
    merged = []
    for p in broker_pos:
        meta = by_sym.get(p.symbol.upper())
        payload = (meta.payload or {}) if meta else {}
        stop_oid = payload.get("stop_order_id")
        ledger_stop = meta.stop_price if meta is not None and meta.stop_price is not None else None
        stop_px = protective_stop_for_display(
            symbol=p.symbol,
            qty=p.qty,
            open_orders=open_orders,
            ledger_stop=ledger_stop,
            stop_order_id=str(stop_oid) if stop_oid else None,
        )
        merged.append(
            {
                "symbol": p.symbol,
                "qty": str(p.qty),
                "avg_entry": str(p.avg_entry),
                "stop_price": str(round_equity_price(stop_px)) if stop_px is not None else None,
                "target_price": str(meta.target_price)
                if meta and meta.target_price
                else (str(p.target_price) if p.target_price else None),
                "strategy_version": meta.strategy_version if meta else None,
                "ledger_id": str(meta.id) if meta else None,
                "opened_at": meta.opened_at.isoformat()
                if meta
                else (p.opened_at.isoformat() if p.opened_at else None),
                "status": "open",
            }
        )
    # Ledger-only rows not yet reflected on broker (edge)
    broker_syms = {p.symbol.upper() for p in broker_pos}
    for row in ledger:
        if row.symbol not in broker_syms:
            merged.append(
                {
                    "symbol": row.symbol,
                    "qty": str(row.qty),
                    "avg_entry": str(row.avg_entry),
                    "stop_price": str(row.stop_price) if row.stop_price else None,
                    "target_price": str(row.target_price) if row.target_price else None,
                    "strategy_version": row.strategy_version,
                    "ledger_id": str(row.id),
                    "opened_at": row.opened_at.isoformat() if row.opened_at else None,
                    "status": row.status,
                }
            )
    await attach_company_names(merged, settings.finnhub_api_key)
    return {"positions": merged, "count": len(merged)}
