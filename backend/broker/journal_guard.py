"""Refuse foreign operational state before Alpaca reconciliation starts."""

from sqlalchemy import select

from database.models.desk import OrderIntentRow
from database.models.positions import OpenPositionRow
from database.session import session_factory
from trading.order_intent import UNRESOLVED

_ALPACA_NAMES = frozenset({"alpaca", "AlpacaPaperBroker"})


def assert_alpaca_journal() -> None:
    """Read-only cutover check. Never relabel IDs, close positions or clear UNKNOWN."""
    with session_factory()() as session:
        unresolved = session.scalars(
            select(OrderIntentRow).where(OrderIntentRow.status.in_([s.value for s in UNRESOLVED]))
        )
        for row in unresolved:
            if row.broker not in _ALPACA_NAMES or row.broker_environment != "paper":
                raise RuntimeError("ALPACA_CUTOVER_BLOCKED_FOREIGN_INTENTS")
        positions = session.scalars(select(OpenPositionRow).where(OpenPositionRow.status == "open"))
        for position in positions:
            if not position.broker_entry_order_id:
                raise RuntimeError("ALPACA_CUTOVER_BLOCKED_POSITION_PROVENANCE")
            entry = session.scalar(
                select(OrderIntentRow).where(
                    OrderIntentRow.broker_order_id == position.broker_entry_order_id,
                    OrderIntentRow.broker.in_(_ALPACA_NAMES),
                    OrderIntentRow.broker_environment == "paper",
                    OrderIntentRow.purpose == "entry",
                    OrderIntentRow.symbol == position.symbol,
                )
            )
            if entry is None:
                raise RuntimeError("ALPACA_CUTOVER_BLOCKED_FOREIGN_POSITION")
