from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from broker.journal_guard import assert_alpaca_journal
from database.models.desk import OrderIntentRow
from database.models.positions import OpenPositionRow
from database.session import session_factory


def add_intent(broker, status):
    with session_factory()() as session:
        session.add(
            OrderIntentRow(
                idempotency_key=str(uuid4()),
                purpose="exit",
                broker=broker,
                broker_environment="paper",
                symbol="AAPL",
                status=status,
                created_at=datetime.now(UTC),
                payload={},
            )
        )
        session.commit()


def test_empty_or_alpaca_journal_can_start():
    assert_alpaca_journal()
    add_intent("AlpacaPaperBroker", "unknown")
    assert_alpaca_journal()  # Alpaca reconciliation must resolve its own UNKNOWN.


def test_foreign_unknown_refuses_without_mutating_history():
    add_intent("retired-venue", "unknown")
    with pytest.raises(RuntimeError, match="FOREIGN_INTENTS"):
        assert_alpaca_journal()
    with session_factory()() as session:
        row = session.scalar(select(OrderIntentRow))
        assert row.status == "unknown"
        assert row.broker == "retired-venue"


def test_terminal_foreign_history_is_preserved_but_not_reconciled():
    add_intent("retired-venue", "canceled")
    assert_alpaca_journal()


@pytest.mark.parametrize("entry_id", [None, "foreign-order-id"])
def test_open_position_without_alpaca_entry_provenance_refuses(entry_id):
    with session_factory()() as session:
        session.add(
            OpenPositionRow(
                symbol="AAPL",
                qty=Decimal(1),
                avg_entry=Decimal(100),
                strategy_version="orb@1.1.0",
                status="open",
                opened_at=datetime.now(UTC),
                broker_entry_order_id=entry_id,
                payload={},
                entry_reasons=[],
            )
        )
        session.commit()
    with pytest.raises(RuntimeError, match="ALPACA_CUTOVER_BLOCKED"):
        assert_alpaca_journal()
    with session_factory()() as session:
        assert session.scalar(select(OpenPositionRow)).status == "open"
