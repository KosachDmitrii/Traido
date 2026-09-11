"""Recover only position-linked, fully accounted broker exits. Never infer P&L."""

import asyncio
import logging
import time
from datetime import UTC, datetime
from decimal import Decimal

from core.enums import OrderSide, OrderStatus
from core.ports import BrokerPort
from database.models.journal import TradeJournalRow
from database.session import session_factory
from trading.ledger import PositionLedger

logger = logging.getLogger(__name__)
_retry_after: dict[str, float] = {}


async def repair_unknown_exits(broker: BrokerPort, ledger: PositionLedger) -> int:
    Session = session_factory(ledger._engine)
    with Session() as db:
        rows = list(
            db.query(TradeJournalRow)
            .filter(TradeJournalRow.backtest_run_id.is_(None), TradeJournalRow.pnl.is_(None))
            .all()
        )
    repaired = 0
    requested = 0
    for row in rows:
        payload = row.assessments_at_entry or {}
        oid = payload.get("stop_order_id")
        # A partially applied exit needs its complete fill ledger, not the last order alone.
        if not oid or payload.get("exit_legs"):
            continue
        key = str(row.id)
        if time.monotonic() < _retry_after.get(key, 0):
            continue
        if requested >= 3:
            break
        requested += 1
        _retry_after[key] = time.monotonic() + 60
        try:
            order = await asyncio.wait_for(broker.get_order(str(oid)), timeout=5)
        except Exception as exc:  # noqa: BLE001 — unreadable broker never supplies a price
            logger.warning(
                "Journal exit verification unavailable: %s (%s)", row.symbol, type(exc).__name__
            )
            continue
        price, qty = order.filled_avg_price, order.filled_qty
        if (
            order.broker_order_id != str(oid)
            or order.symbol != row.symbol
            or order.side != OrderSide.SELL
            or order.status != OrderStatus.FILLED
            or qty is None
            or qty != Decimal(str(row.qty))
            or price is None
            or not price.is_finite()
            or price <= 0
        ):
            continue
        try:
            filled_at = datetime.fromisoformat(order.raw["filled_at"])
            opened = row.opened_at
            if opened is None or filled_at.tzinfo is None:
                continue
            opened = opened.replace(tzinfo=UTC) if opened.tzinfo is None else opened
            if filled_at < opened or filled_at > datetime.now(UTC):
                continue
        except (KeyError, ValueError, TypeError):
            continue
        with Session() as db:
            current = (
                db.query(TradeJournalRow)
                .filter(TradeJournalRow.id == row.id)
                .with_for_update()
                .one()
            )
            if current.pnl is not None:
                continue
            entry = Decimal(str(current.entry))
            current.exit = price
            current.pnl = (price - entry) * qty
            current.pnl_pct = float((price - entry) / entry * 100)
            current.closed_at = filled_at
            current.exit_reasons = ["Broker exit fill verified"]
            current.assessments_at_entry = {
                **payload,
                "verified_exit": {
                    "broker_order_id": str(oid),
                    "qty": str(qty),
                    "price": str(price),
                    "filled_at": filled_at.isoformat(),
                    "recovered_at": datetime.now(UTC).isoformat(),
                },
            }
            db.commit()
        repaired += 1
        _retry_after.pop(key, None)
        logger.info("Journal exit verified: %s price=%s qty=%s", row.symbol, price, qty)
    return repaired
