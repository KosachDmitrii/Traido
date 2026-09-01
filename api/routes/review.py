"""Stage 5 — positions + review analytics."""

from __future__ import annotations

from fastapi import APIRouter

from agents.review.agent import build_review
from broker.factory import create_broker
from core.config import get_settings
from trading.ledger import LEDGER

router = APIRouter(prefix="/api/v1", tags=["review"])


@router.get("/review")
async def review(live_only: bool = True) -> dict:
    """Journal analytics — Review Agent (no trading authority)."""
    return build_review(live_only=live_only).to_dict()


@router.get("/positions")
async def positions() -> dict:
    """Open broker positions + Traido ledger metadata."""
    settings = get_settings()
    broker = create_broker(settings)
    broker_pos = await broker.list_positions()
    ledger = LEDGER.get_open()
    by_sym = {r.symbol: r for r in ledger}
    merged = []
    for p in broker_pos:
        meta = by_sym.get(p.symbol.upper())
        merged.append(
            {
                "symbol": p.symbol,
                "qty": str(p.qty),
                "avg_entry": str(p.avg_entry),
                "stop_price": str(meta.stop_price)
                if meta and meta.stop_price
                else (str(p.stop_price) if p.stop_price else None),
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
    return {"positions": merged, "count": len(merged)}
