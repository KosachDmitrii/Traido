"""P1-12: the database refuses a second open position for one symbol.

The rule was already enforced twice in Python — the execution service refuses
the entry, and the ledger refuses the row under a `threading.Lock`. Both hold
within one process and neither holds across two, and the consequence of losing
it is not recoverable by reading harder: two open rows for one symbol each carry
a protective stop for shares the other row also claims, so the book cannot be
reconciled against a broker that reports a single net position.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.schemas import PortfolioSnapshot
from database.base import Base
from database.models.positions import OpenPositionRow


@pytest.fixture
def session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'positions.db'}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as s:
        yield s


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        equity=Decimal(100_000),
        cash=Decimal(100_000),
        buying_power=Decimal(100_000),
        open_exposure=Decimal(0),
        open_positions=0,
        day_pnl=Decimal(0),
        week_pnl=Decimal(0),
        drawdown_pct=0.0,
        kill_switch=False,
    )


def _row(symbol: str, *, status: str = "open") -> OpenPositionRow:
    return OpenPositionRow(
        id=uuid.uuid4(),
        symbol=symbol,
        qty=Decimal(10),
        avg_entry=Decimal(100),
        strategy_version="test@1",
        status=status,
        entry_reasons=[],
        payload={},
        opened_at=datetime.now(UTC),
    )


def test_a_second_open_row_for_one_symbol_is_refused(session) -> None:
    session.add(_row("AAPL"))
    session.commit()

    session.add(_row("AAPL"))
    with pytest.raises(IntegrityError):
        session.commit()


def test_two_symbols_may_both_be_open(session) -> None:
    session.add_all([_row("AAPL"), _row("MSFT")])
    session.commit()

    assert session.query(OpenPositionRow).count() == 2


def test_closed_rows_for_one_symbol_accumulate_freely(session) -> None:
    """A symbol traded repeatedly leaves a history, and history is not a conflict."""
    session.add_all([_row("AAPL", status="closed") for _ in range(3)])
    session.commit()

    assert session.query(OpenPositionRow).count() == 3


def test_a_symbol_can_be_re_entered_after_the_position_closes(session) -> None:
    first = _row("AAPL")
    session.add(first)
    session.commit()

    first.status = "closed"
    session.commit()
    session.add(_row("AAPL"))
    session.commit()

    still_open = (
        session.query(OpenPositionRow)
        .filter(OpenPositionRow.symbol == "AAPL", OpenPositionRow.status == "open")
        .count()
    )
    assert still_open == 1


def test_the_index_is_partial_rather_than_plain(session) -> None:
    """A plain unique index would reject every re-entry the desk has ever made."""
    sql = session.execute(
        text(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND name = 'ux_open_positions_one_open_per_symbol'"
        )
    ).scalar_one()

    assert "WHERE" in sql.upper(), sql


def test_the_ledger_reports_a_cross_process_clash_as_a_duplicate(tmp_path) -> None:
    """The in-process lock does not span workers, so the index has to be caught.

    A second API worker, or the scanner in its own container, passes the
    ledger's read check cleanly — it holds a different lock and read before the
    other process inserted. The index stops it, and the entry path has to
    receive the refusal it already knows how to handle rather than an
    `IntegrityError` escaping from the commit.
    """
    from sqlalchemy import create_engine

    from core.enums import TradeAction, TradingMode
    from core.schemas import TradeCandidate
    from risk.risk_engine import RiskEngine
    from tests.support import CLEARED_EARNINGS
    from trading.ledger import DuplicateOpenPosition, PositionLedger
    from trading.opportunities import MemoryOpportunityStore

    engine = create_engine(f"sqlite:///{tmp_path / 'ledger.db'}", future=True)
    Base.metadata.create_all(engine)
    with Session(engine, future=True) as planted:
        planted.add(_row("AAPL"))
        planted.commit()

    class _BlindWorker(PositionLedger):
        """A process whose read of the book predates the other's insert."""

        def _open_clash(self, session, symbol):
            return None

    candidate = TradeCandidate(
        symbol="AAPL",
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal(100),
        stop=Decimal(95),
        target=Decimal(120),
        risk_reward=4.0,
        reasons=["fixture"],
        strategy_version="test-v1",
    )
    opp = MemoryOpportunityStore().create(
        candidate,
        RiskEngine().evaluate(candidate, _portfolio(), context=CLEARED_EARNINGS),
        TradingMode.CONFIRMATION,
    )

    with pytest.raises(DuplicateOpenPosition, match="AAPL"):
        _BlindWorker(engine).open_from_opportunity(
            opp, qty=Decimal(5), broker_entry_order_id="b-1", fill_price=Decimal(100)
        )
