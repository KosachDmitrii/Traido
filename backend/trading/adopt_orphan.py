"""Adopt broker positions missing from the Traido ledger."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import Settings, get_settings
from core.enums import IntentPurpose, IntentStatus, OpportunityStatus
from core.ports import BrokerPort
from database.models.desk import OpportunityRow, OrderIntentRow
from database.session import session_factory
from trading.desk_positions import resolve_protective_stop
from trading.intents import OrderIntentStore
from trading.ledger import DuplicateOpenPosition, PositionLedger

logger = logging.getLogger(__name__)

_ORPHAN_PREFIX = "orphan:"


def _session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return session_factory(engine)


def _latest_executed_opportunity(session: Session, symbol: str) -> OpportunityRow | None:
    return (
        session.query(OpportunityRow)
        .filter(
            OpportunityRow.symbol == symbol.upper(),
            OpportunityRow.status == OpportunityStatus.EXECUTED.value,
        )
        .order_by(OpportunityRow.created_at.desc())
        .first()
    )


def _latest_entry_fill(session: Session, symbol: str) -> OrderIntentRow | None:
    return (
        session.query(OrderIntentRow)
        .filter(
            OrderIntentRow.symbol == symbol.upper(),
            OrderIntentRow.purpose == IntentPurpose.ENTRY.value,
            OrderIntentRow.status == IntentStatus.FILLED.value,
        )
        .order_by(OrderIntentRow.updated_at.desc())
        .first()
    )


def _protective_stop_order_id(intents: OrderIntentStore, symbol: str) -> str | None:
    matches = [
        intent
        for intent in intents.list_by_key_prefix("protection:")
        if intent.symbol.upper() == symbol.upper() and intent.broker_order_id
    ]
    if not matches:
        return None
    return matches[-1].broker_order_id


def clear_orphan_blocks(intents: OrderIntentStore, symbol: str) -> int:
    """Drop orphan UNKNOWN intents once the book explains the broker position."""
    ticker = symbol.upper()
    cleared = 0
    for intent in intents.list_by_key_prefix(f"{_ORPHAN_PREFIX}{ticker}:"):
        if not intent.is_unresolved:
            continue
        intents.transition(
            intent.id,
            IntentStatus.CANCELED,
            last_error="adopted into ledger",
        )
        cleared += 1
    return cleared


async def adopt_orphan_position(
    *,
    symbol: str,
    broker: BrokerPort,
    ledger: PositionLedger,
    intents: OrderIntentStore,
    settings: Settings | None = None,
    dry_run: bool = False,
    require_correlation: bool = True,
    operator_override: bool = False,
    expected_broker_order_id: str | None = None,
    expected_client_order_id: str | None = None,
    expected_broker_perm_id: str | None = None,
) -> dict[str, Any]:
    """Backfill ledger only with proven broker correlation (or explicit override).

    Spec: never adopt from "latest Opportunity/intent by symbol" alone.
    """
    from core.metrics import METRICS

    settings = settings or get_settings()
    ticker = symbol.upper()

    existing = ledger.find_open_by_symbol(ticker)
    if existing is not None:
        return {
            "status": "already_open",
            "symbol": ticker,
            "position_id": str(existing.id),
            "qty": str(existing.qty),
            "avg_entry": str(existing.avg_entry),
            "stop_price": str(existing.stop_price) if existing.stop_price is not None else None,
            "target_price": str(existing.target_price)
            if existing.target_price is not None
            else None,
        }

    broker_pos = next(
        (p for p in await broker.list_positions() if p.symbol.upper() == ticker),
        None,
    )
    if broker_pos is None:
        return {"status": "error", "symbol": ticker, "reason": "no_broker_position"}

    qty = Decimal(str(broker_pos.qty))
    avg_entry = Decimal(str(broker_pos.avg_entry))
    if qty <= 0:
        return {"status": "error", "symbol": ticker, "reason": "broker_qty_zero"}

    open_orders = await broker.list_open_orders()
    known_stop_oid = _protective_stop_order_id(intents, ticker)

    SessionLocal = _session_factory(ledger._engine)
    candidate: dict[str, Any] | None = None
    opportunity_id: UUID | None = None
    trading_mode_from_card: str | None = None
    broker_entry_order_id: str | None = None
    correlation_evidence: dict[str, Any] = {}
    opened_at = broker_pos.opened_at
    with SessionLocal() as session:
        entry_intent = _latest_entry_fill(session, ticker)
        # Correlation: match explicit IDs — never "last opportunity by symbol".
        correlated = False
        if entry_intent is not None:
            if (
                expected_broker_order_id
                and entry_intent.broker_order_id == expected_broker_order_id
            ):
                correlated = True
                correlation_evidence["broker_order_id"] = expected_broker_order_id
            payload = entry_intent.payload or {}
            client_oid = payload.get("client_order_id") or getattr(
                entry_intent, "client_order_id", None
            )
            perm_id = payload.get("broker_perm_id") or getattr(entry_intent, "broker_perm_id", None)
            if expected_client_order_id and client_oid == expected_client_order_id:
                correlated = True
                correlation_evidence["client_order_id"] = expected_client_order_id
            if expected_broker_perm_id and perm_id == expected_broker_perm_id:
                correlated = True
                correlation_evidence["broker_perm_id"] = expected_broker_perm_id

        if require_correlation and not operator_override and not correlated:
            METRICS.counter(
                "external_position_correlation_failed",
                help_text="Adoption refused without broker/client/perm correlation",
            )
            return {
                "status": "error",
                "symbol": ticker,
                "reason": "correlation_required",
            }

        if correlated and entry_intent is not None:
            broker_entry_order_id = entry_intent.broker_order_id
            if entry_intent.updated_at is not None:
                opened_at = entry_intent.updated_at
            if entry_intent.opportunity_id is not None:
                opp_row = session.get(OpportunityRow, entry_intent.opportunity_id)
                if opp_row is not None:
                    opportunity_id = opp_row.id
                    card = opp_row.payload or {}
                    cand = card.get("candidate")
                    if isinstance(cand, dict):
                        candidate = dict(cand)
                    trading_mode_from_card = str(
                        card.get("trading_mode") or settings.trading_mode.value
                    )

    if opened_at is not None and opened_at.tzinfo is None:
        opened_at = opened_at.replace(tzinfo=UTC)

    stop_price, stop_order_id = resolve_protective_stop(
        symbol=ticker,
        qty=qty,
        open_orders=open_orders,
        stop_order_id=known_stop_oid,
    )

    target_price: Decimal | None
    if candidate is not None:
        stop_price = stop_price or Decimal(str(candidate["stop"]))
        target_price = Decimal(str(candidate["target"]))
        strategy_version = str(candidate.get("strategy_version") or "adopted_orphan")
        entry_reasons = list(candidate.get("reasons") or [])
        trading_mode = trading_mode_from_card or settings.trading_mode.value
        payload = {
            "confidence": candidate.get("confidence"),
            "card_risk_reward": candidate.get("risk_reward"),
            "planned_entry": str(candidate.get("entry")),
            "pipeline_run_id": candidate.get("pipeline_run_id"),
            "exec_timeframe": (
                candidate.get("exec_timeframe", {}).get("value")
                if isinstance(candidate.get("exec_timeframe"), dict)
                else candidate.get("exec_timeframe")
            ),
            "adopted": True,
            "adopted_at": datetime.now(UTC).isoformat(),
            "correlation_evidence": correlation_evidence,
            "operator_override": operator_override,
        }
    else:
        stop_price = stop_price or broker_pos.stop_price
        target_price = broker_pos.target_price
        strategy_version = "adopted_orphan"
        entry_reasons = ["Adopted: broker position with no executed opportunity row"]
        opportunity_id = None
        trading_mode = settings.trading_mode.value
        payload = {
            "adopted": True,
            "adopted_at": datetime.now(UTC).isoformat(),
            "correlation_evidence": correlation_evidence,
            "operator_override": operator_override,
        }

    plan: dict[str, Any] = {
        "status": "planned" if dry_run else "adopted",
        "symbol": ticker,
        "qty": str(qty),
        "avg_entry": str(avg_entry),
        "stop_price": str(stop_price) if stop_price is not None else None,
        "target_price": str(target_price) if target_price is not None else None,
        "stop_order_id": stop_order_id,
        "strategy_version": strategy_version,
        "opportunity_id": str(opportunity_id) if opportunity_id else None,
        "broker_entry_order_id": broker_entry_order_id,
        "orphan_blocks_cleared": 0,
    }

    if dry_run:
        plan["orphan_blocks_cleared"] = len(
            [i for i in intents.list_by_key_prefix(f"{_ORPHAN_PREFIX}{ticker}:") if i.is_unresolved]
        )
        return plan

    try:
        row = ledger.adopt_broker_position(
            symbol=ticker,
            qty=qty,
            avg_entry=avg_entry,
            stop_price=stop_price,
            target_price=target_price,
            strategy_version=strategy_version,
            trading_mode=trading_mode,
            entry_reasons=entry_reasons,
            opportunity_id=opportunity_id,
            broker_entry_order_id=broker_entry_order_id,
            stop_order_id=str(stop_order_id) if stop_order_id else None,
            payload=payload,
            opened_at=opened_at,
        )
    except DuplicateOpenPosition as exc:
        return {"status": "error", "symbol": ticker, "reason": str(exc)}

    cleared = clear_orphan_blocks(intents, ticker)
    plan.update(
        {
            "status": "adopted",
            "position_id": str(row.id),
            "orphan_blocks_cleared": cleared,
        }
    )
    logger.info("adopt_orphan %s -> %s", ticker, plan)
    return plan
