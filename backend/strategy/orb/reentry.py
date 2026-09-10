"""Versioned lifecycle policy: a closed trade may seed a NEW, freshly admitted plan."""

from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from core.enums import BrokerEnvironment, OpportunityStatus
from core.schemas import Bar
from database.models.desk import OpportunityRow
from database.models.journal import TradeJournalRow
from database.models.orb import OrbSessionRow
from database.models.positions import OpenPositionRow
from database.session import session_factory
from strategy.orb import OrbPlan, form_plan

POLICY = "closed-position-reentry@1"


def closed_position(db, opportunity_id):
    """Exact entry linkage plus a completed journal row; absence is not a close."""
    rows = list(
        db.scalars(select(OpenPositionRow).where(OpenPositionRow.opportunity_id == opportunity_id))
    )
    if len(rows) != 1:
        return None
    pos = rows[0]
    if pos.status != "closed" or pos.qty != 0 or pos.closed_at is None:
        return None
    journal = db.scalar(
        select(TradeJournalRow).where(
            TradeJournalRow.position_id == pos.id,
            TradeJournalRow.backtest_run_id.is_(None),
            TradeJournalRow.closed_at.is_not(None),
        )
    )
    return pos if journal is not None else None


async def rearm_closed_trade(day: str, symbol: str, broker, *, now: datetime) -> bool:
    from core.config import get_settings
    from trading.intents import INTENTS

    if get_settings().broker_env is not BrokerEnvironment.PAPER:
        return False
    with session_factory()() as db:
        row = db.get(OrbSessionRow, day)
        state = row.payload.get("states", {}).get(symbol, {}) if row else {}
        oid = state.get("opportunity_id")
        if not oid:
            return False
        oid = UUID(oid)
        opp = db.get(OpportunityRow, oid)
        if opp is None or opp.status != OpportunityStatus.EXECUTED.value:
            return False
        closed = closed_position(db, oid)
        if closed is None:
            return False
        position_id = closed.id
        # Database DateTime timestamps are UTC; SQLite strips tzinfo in tests.
        after = (
            closed.closed_at.replace(tzinfo=UTC)
            if closed.closed_at.tzinfo is None
            else closed.closed_at
        )
        expected = deepcopy(row.payload["plans"][symbol])
    plan = OrbPlan.model_validate(expected)
    if now >= plan.entry_deadline or after > now or symbol in INTENTS.unresolved_symbols():
        return False
    # Fresh broker reads, not the dashboard cache. Failures leave the claim intact.
    positions = await broker.list_positions()
    orders = await broker.list_open_orders()
    if any(p.symbol.upper() == symbol and p.qty != 0 for p in positions) or any(
        o.symbol.upper() == symbol for o in orders
    ):
        return False
    rebuilt = form_plan(
        symbol,
        [Bar.model_validate(b) for b in plan.evidence["daily"]],
        [Bar.model_validate(b) for b in plan.evidence["opening"]],
        now=now,
        feed=plan.source.removeprefix("alpaca:"),
    )
    if rebuilt.plan is None:
        return False
    replacement = rebuilt.plan.model_dump(mode="json")
    replacement["evidence"]["reentry"] = {
        "policy": POLICY,
        "previous_opportunity_id": str(oid),
        "closed_position_id": str(position_id),
        "closed_at": after.isoformat(),
        "verified_at": now.isoformat(),
    }
    with session_factory()() as db:
        row = db.scalar(select(OrbSessionRow).where(OrbSessionRow.session == day).with_for_update())
        if (
            row is None
            or row.payload.get("plans", {}).get(symbol) != expected
            or row.payload.get("states", {}).get(symbol, {}).get("opportunity_id") != str(oid)
        ):
            return False
        opp = db.scalar(select(OpportunityRow).where(OpportunityRow.id == oid).with_for_update())
        closed = closed_position(db, oid)
        if (
            opp is None
            or opp.status != OpportunityStatus.EXECUTED.value
            or closed is None
            or closed.id != position_id
        ):
            return False
        if (
            db.scalar(
                select(OpenPositionRow.id).where(
                    OpenPositionRow.symbol == symbol, OpenPositionRow.status == "open"
                )
            )
            or symbol in INTENTS.unresolved_symbols()
        ):
            return False
        payload = deepcopy(row.payload)
        prior = payload["states"][symbol]
        history = [*prior.get("closed_opportunity_ids", []), str(oid)]
        payload["plans"][symbol] = replacement
        payload["states"][symbol] = {
            "state": "WAIT",
            "reasons": ["ORB_CLOSED_WAITING_NEW_SIGNAL"],
            "closed_opportunity_ids": history,
            "retest_after": after.isoformat(),
            "rearmed_at": now.isoformat(),
            "reentry_policy": POLICY,
        }
        row.payload = payload
        db.commit()
    return True
