"""Recover attribution of broker exposure from durable approval evidence only.

Includes terminal intents, because older reconciliation marked FILLED before
creating a position. No new entry orders are submitted by this module.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from core.ports import AuditPort, BrokerPort
from core.schemas import Position
from trading.intents import OrderIntentStorePort

if TYPE_CHECKING:
    from trading.reconcile import ReconciliationReport

from broker.interface import resolve_broker_identity
from core.enums import IntentPurpose, OrderSide, OrderType
from trading.admission_records import ADMISSION_RECORDS
from trading.entry_activity import entry_active
from trading.geometry_hash import compute_geometry_hash
from trading.ledger import DuplicateOpenPosition, PositionLedger
from trading.opportunities import OPPORTUNITIES
from trading.order_intent import OrderIntent, locate_broker_order


async def recover_entry_positions(
    broker: BrokerPort,
    intents: OrderIntentStorePort,
    ledger: PositionLedger,
    positions: dict[str, Position],
    report: ReconciliationReport,
    audit: AuditPort | None = None,
) -> None:
    try:
        name, account, environment = resolve_broker_identity(broker)
    except RuntimeError:
        report.unresolved.append("entry_recovery:broker_identity_unverified")
        return
    candidates: dict[str, list[OrderIntent]] = {}
    for intent in intents.list_by_key_prefix("entry:"):
        if intent.purpose != IntentPurpose.ENTRY or intent.side != OrderSide.BUY:
            continue
        if intent.symbol not in positions or ledger.find_open_by_symbol(intent.symbol):
            continue
        if intent.broker != name or intent.broker_environment != environment:
            continue
        candidates.setdefault(intent.symbol, []).append(intent)
    for symbol, history in candidates.items():
        matches = []
        for intent in history:
            if entry_active(intent.opportunity_id):
                continue
            order = await locate_broker_order(broker, intent)
            if order is None or not order.filled_qty or order.filled_qty <= 0:
                continue
            if not order.broker_order_id or ledger.find_by_entry_order(order.broker_order_id):
                continue
            matches.append((intent, order))
        if len(matches) != 1:
            if matches:
                report.unresolved.append(f"entry_recovery:{symbol}:ambiguous_fills")
            continue
        intent, order = matches[0]
        prefix = f"entry_recovery:{symbol}:"
        if entry_active(intent.opportunity_id):
            continue
        if not intent.broker_account_id or intent.broker_account_id != account:
            report.unresolved.append(prefix + "account_unverified")
            continue
        if (
            order.symbol != symbol
            or order.side != OrderSide.BUY
            or order.client_order_id != intent.client_order_id
            or order.filled_qty != positions[symbol].qty
            or order.filled_qty > intent.requested_qty
            or order.filled_avg_price is None
            or order.filled_avg_price <= 0
        ):
            report.unresolved.append(prefix + "execution_mismatch")
            continue
        opp = OPPORTUNITIES.get(intent.opportunity_id) if intent.opportunity_id else None
        record = (
            ADMISSION_RECORDS.get(intent.approval_admission_record_id)
            if intent.approval_admission_record_id
            else None
        )
        if (
            opp is None
            or record is None
            or not record.admitted
            or record.phase != "approval"
            or record.symbol != symbol
            or opp.candidate.symbol != symbol
            or record.opportunity_id != opp.id
            or opp.approval_admission_record_id != record.id
            or not intent.geometry_hash
            or record.geometry_hash != intent.geometry_hash
        ):
            report.unresolved.append(prefix + "approval_unverified")
            continue
        c = opp.candidate
        if c.exec_timeframe is None:
            report.unresolved.append(prefix + "timeframe_unverified")
            continue
        geometry = compute_geometry_hash(
            entry=str(c.entry),
            stop=str(c.stop),
            target=str(c.target),
            exec_timeframe=c.exec_timeframe.value,
            strategy_version=c.strategy_version,
        )
        if geometry != intent.geometry_hash or not (
            Decimal(0) < c.stop < order.filled_avg_price < c.target
        ):
            report.unresolved.append(prefix + "geometry_mismatch")
            continue
        try:
            orders = await broker.list_open_orders()
        except Exception:  # noqa: BLE001 — unknown protection must block recovery
            report.unresolved.append(prefix + "orders_unverified")
            continue
        sells = [o for o in orders if o.symbol == symbol and o.side == OrderSide.SELL]
        stop_id = None
        if sells:
            if (
                len(sells) != 1
                or sells[0].order_type != OrderType.STOP
                or sells[0].qty != order.filled_qty
                or sells[0].stop_price != c.stop
                or not sells[0].broker_order_id
            ):
                report.unresolved.append(prefix + "protection_conflict")
                continue
            stop_id = sells[0].broker_order_id
        elif any(i.symbol == symbol and i.is_exit for i in intents.list_unresolved()):
            report.unresolved.append(prefix + "exit_unresolved")
            continue
        try:
            current = [p for p in await broker.list_positions() if p.symbol == symbol]
        except Exception:  # noqa: BLE001
            report.unresolved.append(prefix + "position_unverified")
            continue
        if len(current) != 1 or current[0].qty != order.filled_qty:
            report.unresolved.append(prefix + "position_changed")
            continue
        # No await between the ownership check and durable insertion. The normal
        # execution path can never race this insert in the single-worker runtime.
        if entry_active(opp.id):
            continue
        try:
            row = ledger.open_from_opportunity(
                opp,
                qty=order.filled_qty,
                broker_entry_order_id=order.broker_order_id,
                fill_price=order.filled_avg_price,
                stop_order_id=stop_id,
            )
        except DuplicateOpenPosition:
            report.unresolved.append(prefix + "position_conflict")
            continue
        intents.update_fields(
            intent.id,
            position_id=row.id,
            filled_qty=order.filled_qty,
            average_fill_price=order.filled_avg_price,
            stop_order_id=stop_id,
            broker_order_id=order.broker_order_id,
        )
        report.changed.append(prefix + "position_restored")
        report.unresolved.append(prefix + "protection_requires_verification")
        if audit:
            await audit.append(
                "EntryPositionRecovered",
                "reconcile",
                {
                    "intent_id": str(intent.id),
                    "position_id": str(row.id),
                    "order_id": order.broker_order_id,
                    "symbol": symbol,
                    "approval_id": str(record.id),
                    "qty": str(order.filled_qty),
                    "stop": str(c.stop),
                    "target": str(c.target),
                },
            )
