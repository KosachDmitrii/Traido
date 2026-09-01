"""A database whose schema has fallen behind the models must not start the API.

This is not hypothetical. The journal database was created once by
`create_all`, migration 0005 later added `order_intents.purpose`, and
`create_all` — which adds tables but never alters one — reported success on
every subsequent start. Nothing failed until reconciliation queried the missing
column, and that failure was a warning behind a 200 response: the desk looked
healthy while it had stopped checking itself against the broker.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Column, MetaData, String, Table, create_engine, text

from database.base import Base
from database.session import init_db, schema_drift


def _fresh_engine(tmp_path):
    return create_engine(f"sqlite:///{tmp_path / 'journal.db'}", future=True)


def _drop_purpose(engine) -> None:
    """Rewind `order_intents` to its pre-0005 shape, indexes included."""
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS ix_order_intents_purpose"))
        conn.execute(text("ALTER TABLE order_intents DROP COLUMN purpose"))


def test_a_freshly_created_database_has_no_drift(tmp_path) -> None:
    """The check must be quiet on the schema `create_all` itself produces."""
    engine = _fresh_engine(tmp_path)
    Base.metadata.create_all(engine)

    assert schema_drift(engine) == []


def test_a_missing_column_is_reported_with_its_table(tmp_path) -> None:
    """Name the column, because "run the migrations" alone does not locate it."""
    engine = _fresh_engine(tmp_path)
    Base.metadata.create_all(engine)
    _drop_purpose(engine)

    assert schema_drift(engine) == ["order_intents.purpose"]


def test_a_missing_table_is_reported(tmp_path) -> None:
    engine = _fresh_engine(tmp_path)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE order_intents"))

    assert "order_intents" in schema_drift(engine)


def test_create_all_does_not_repair_a_stale_table(tmp_path) -> None:
    """The premise of the whole check: `create_all` cannot add a column.

    If SQLAlchemy ever started altering existing tables this test would go
    green in a way that makes the drift check redundant — which is worth
    knowing, rather than discovering by deleting it.
    """
    engine = _fresh_engine(tmp_path)
    stale = MetaData()
    Table("order_intents", stale, Column("id", String, primary_key=True))
    stale.create_all(engine)

    Base.metadata.create_all(engine)

    assert "order_intents.purpose" in schema_drift(engine)


def test_init_db_refuses_to_start_on_a_stale_database(tmp_path) -> None:
    """Loud beats convenient. A half-migrated book is not a lesser book."""
    engine = _fresh_engine(tmp_path)
    Base.metadata.create_all(engine)
    _drop_purpose(engine)

    with pytest.raises(RuntimeError) as err:
        init_db(engine)

    assert "order_intents.purpose" in str(err.value)
    assert "alembic upgrade head" in str(err.value)


def test_init_db_accepts_a_database_it_just_created(tmp_path) -> None:
    engine = _fresh_engine(tmp_path)

    assert init_db(engine) is engine


def test_a_missing_unique_index_is_drift(tmp_path) -> None:
    """One-open-position-per-symbol lives in an index, so its absence is schema drift.

    A database missing it behaves identically to one that has it, right up until
    the day two rows appear for one symbol — at which point the book can no
    longer be reconciled against a broker reporting a single net position. That
    is not a state to discover from a position report.
    """
    engine = _fresh_engine(tmp_path)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ux_open_positions_one_open_per_symbol"))

    assert schema_drift(engine) == ["open_positions index ux_open_positions_one_open_per_symbol"]


def test_a_local_database_gains_an_index_declared_after_it_was_created(tmp_path) -> None:
    """`create_all` never revisits a table it has already built.

    So a developer's database from last week keeps enforcing last week's
    constraints while the code believes otherwise — a desk that is quietly
    laxer than production is the worst place to find out an invariant holds.
    """
    engine = _fresh_engine(tmp_path)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ux_open_positions_one_open_per_symbol"))

    init_db(engine)

    assert schema_drift(engine) == []


def test_a_local_database_that_already_violates_the_index_is_named_not_forced(tmp_path) -> None:
    """Two open rows for one symbol is a book, not a schema problem.

    Only a human can say which row the broker actually holds, so the start-up
    refuses and says so rather than dropping a row to make the index fit.
    """
    import uuid
    from datetime import UTC, datetime

    engine = _fresh_engine(tmp_path)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ux_open_positions_one_open_per_symbol"))
        for _ in range(2):
            conn.execute(
                text(
                    "INSERT INTO open_positions (id, symbol, qty, avg_entry, strategy_version, "
                    "trading_mode, status, entry_reasons, payload, opened_at, created_at) "
                    "VALUES (:id, 'AAPL', 10, 100, 'v1', 'confirmation', 'open', '[]', '{}', "
                    ":ts, :ts)"
                ),
                {"id": str(uuid.uuid4()), "ts": datetime.now(UTC).isoformat()},
            )

    with pytest.raises(RuntimeError) as err:
        init_db(engine)

    assert "ux_open_positions_one_open_per_symbol" in str(err.value)
    assert "by hand" in str(err.value)


def test_an_ordinary_index_is_not_treated_as_drift(tmp_path) -> None:
    """Scoped to unique indexes on purpose.

    A missing ordinary index costs query time; a missing unique one costs a
    guarantee. Reporting both would make a refusal-to-start depend on
    performance tuning, and the first spurious failure is what teaches an
    operator to skip the check.
    """
    engine = _fresh_engine(tmp_path)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_open_positions_status"))

    assert schema_drift(engine) == []
