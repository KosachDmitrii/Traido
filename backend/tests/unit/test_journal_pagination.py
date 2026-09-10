"""Journal pages cover all actual trades beyond the analytics window."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import create_engine

from agents.review.agent import journal_page
from database.models.journal import TradeJournalRow
from database.session import init_db, session_factory


def test_complete_history(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pages.db'}")
    init_db(engine)
    with session_factory(engine)() as session:
        for i in range(208):
            session.add(
                TradeJournalRow(
                    symbol=f"T{i}",
                    entry=100,
                    exit=101,
                    qty=1,
                    pnl=1,
                    pnl_pct=1,
                    strategy_version="orb@1.1.0",
                    closed_at=datetime(2026, 9, 10, tzinfo=UTC),
                    backtest_run_id=uuid4() if i >= 205 else None,
                )
            )
        session.commit()
    pages = [journal_page(page=i, page_size=25, engine=engine) for i in range(1, 10)]
    assert all(p["total"] == 205 and p["page_count"] == 9 for p in pages)
    assert [len(p["items"]) for p in pages] == [25] * 8 + [5]
    assert len({r["id"] for p in pages for r in p["items"]}) == 205
    assert all(r["symbol"] not in {"T205", "T206", "T207"} for p in pages for r in p["items"])
    assert journal_page(page=1, page_size=25, engine=engine)["items"] == pages[0]["items"]
    assert journal_page(page=100, page_size=25, engine=engine)["page"] == 9
    assert len(journal_page(page_size=100, engine=engine)["items"]) == 100


def test_empty_history(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    init_db(engine)
    assert journal_page(page=9, engine=engine) == {
        "items": [],
        "total": 0,
        "page": 1,
        "page_size": 10,
        "page_count": 1,
    }
