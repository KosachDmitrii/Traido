"""Checking the schema is a startup job, not something every query redoes.

Each store built its session factory by calling `init_db` inline, so a full
inspection ran on every individual operation: `create_all`, an index sweep, then
`get_columns` for every table in the metadata. Dozens of PRAGMA round trips per
audit append and per intent read, on the path an order is placed through.

The cost was the smaller half. Reflection consumes PRAGMA cursors, so two
threads inspecting one table over one connection tear each other's results
apart — `test_22`, which fires two approvals at once, failed about one run in
ten with `IndexError: tuple index out of range` raised from inside SQLAlchemy's
SQLite dialect. A concurrency bug in schema *reflection* is close to unreadable
from that traceback; it looks like a corrupt database.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from database import session as session_mod
from database.session import session_factory


@pytest.fixture
def engine():
    eng = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    yield eng
    eng.dispose()


def test_one_engine_is_prepared_once(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    real = session_mod.init_db

    def _counted(eng=None):
        nonlocal calls
        calls += 1
        return real(eng)

    monkeypatch.setattr(session_mod, "init_db", _counted)

    for _ in range(20):
        session_factory(engine)

    assert calls == 1


def test_a_second_engine_is_prepared_too(engine) -> None:
    """Caching per engine, not once per process.

    Tests repoint the desk's stores at in-memory databases; a cache that
    remembered "prepared" globally would leave the second one without tables.
    """
    from database.models.desk import OrderIntentRow

    other = create_engine("sqlite://", future=True, poolclass=StaticPool)
    try:
        session_factory(engine)
        with session_factory(other)() as session:
            assert session.query(OrderIntentRow).all() == []
    finally:
        other.dispose()


def test_the_factory_still_works_after_the_first_call(engine) -> None:
    """Preparation is cached; the factory itself is rebuilt and must be usable."""
    session_factory(engine)

    with session_factory(engine)() as session:
        assert session.get_bind() is engine


def test_concurrent_first_use_does_not_race(engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shape of the `test_22` failure, reduced to its cause.

    Eight threads reaching a never-prepared engine at the same moment. Before
    the lock, several entered reflection together and read each other's PRAGMA
    cursors.
    """
    calls = 0
    real = session_mod.init_db

    def _counted(eng=None):
        nonlocal calls
        calls += 1
        return real(eng)

    monkeypatch.setattr(session_mod, "init_db", _counted)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: session_factory(engine), range(8)))

    assert calls == 1


def test_preparation_does_not_pin_the_engine(engine) -> None:
    """Weakly keyed, so a test's throwaway engine is collectable.

    A strong reference here would accumulate one live engine — and for SQLite,
    one open connection — per test in the suite.
    """
    import gc
    import weakref

    throwaway = create_engine("sqlite://", future=True, poolclass=StaticPool)
    session_factory(throwaway)
    ref = weakref.ref(throwaway)

    throwaway.dispose()
    del throwaway
    gc.collect()

    assert ref() is None
