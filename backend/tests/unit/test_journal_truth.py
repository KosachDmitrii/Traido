from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from agents.review.agent import build_review, journal_page
from core.enums import OrderSide, OrderStatus, OrderType
from core.schemas import OrderRecord
from database.session import init_db
from trading.journal_recovery import repair_unknown_exits
from trading.ledger import PositionLedger


@pytest.fixture
def pending(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'journal.db'}")
    init_db(engine)
    ledger = PositionLedger(engine)
    ledger.adopt_broker_position(
        symbol="VZ",
        qty=D(100),
        avg_entry=D("50.60"),
        stop_price=D("50.52"),
        target_price=D("50.90"),
        strategy_version="orb@2.0.0",
        trading_mode="confirmation",
        entry_reasons=[],
        stop_order_id="stop-vz",
        opened_at=datetime.now(UTC) - timedelta(minutes=20),
    )
    journal = ledger.close_and_journal(
        symbol="VZ", exit_price=None, exit_reasons=["Broker flat; exit price unverified"]
    )
    assert journal.pnl is None and journal.exit is None
    assert ledger.get_open() == []
    return ledger, engine


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {},
        {"symbol": "OTHER"},
        {"filled_qty": D(99)},
        {"filled_avg_price": None},
        {"side": OrderSide.BUY},
        {"raw": {}},
        {"broker_order_id": "wrong"},
    ],
)
async def test_recovery_requires_exact_link_quantity_and_fill(pending, change):
    ledger, engine = pending
    order = OrderRecord(
        id=uuid4(),
        client_order_id="stop",
        broker_order_id="stop-vz",
        symbol="VZ",
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        qty=D(100),
        status=OrderStatus.FILLED,
        filled_qty=D(100),
        filled_avg_price=D("50.52"),
        raw={"filled_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat()},
    )
    broker = SimpleNamespace(get_order=AsyncMock(return_value=order.model_copy(update=change)))
    assert await repair_unknown_exits(broker, ledger) == (0 if change else 1)
    page = journal_page(engine=engine)
    if change:
        assert page["items"][0]["pnl"] is None
        assert page["unverified_count"] == 1
        assert build_review(engine=engine).trade_count == 0
    else:
        assert D(page["items"][0]["pnl"]) == D(-8)
        assert page["unverified_count"] == 0
        assert build_review(engine=engine).trade_count == 1
        assert await repair_unknown_exits(broker, ledger) == 0
        assert page["total"] == 1


def test_migration_quarantines_only_estimated_results(pending):
    import runpy

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    from database.models.journal import TradeJournalRow
    from database.session import session_factory

    _ledger, engine = pending
    with session_factory(engine)() as db:
        row = db.query(TradeJournalRow).one()
        row.exit, row.pnl, row.pnl_pct = D("50.60"), D(0), 0
        row.exit_reasons = ["Reconcile: broker flat (stop or external close)"]
        db.commit()
    migration = runpy.run_path("alembic/versions/0018_verified_exit_prices.py")
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        migration["upgrade"]()
    page = journal_page(engine=engine)
    assert page["items"][0]["exit"] is None and page["items"][0]["pnl"] is None
