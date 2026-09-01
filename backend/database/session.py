"""Sync SQLAlchemy engine for journal / backtests (Stage 2)."""

from __future__ import annotations

import os
from collections.abc import MutableSet
from functools import lru_cache
from pathlib import Path
from threading import Lock
from weakref import WeakSet

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from database.base import Base


def _default_sqlite_url() -> str:
    data_dir = Path(__file__).resolve().parents[1] / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{data_dir / 'traido_journal.db'}"


def resolve_sync_database_url(url: str | None = None) -> str:
    """
    Journal DB resolution (Stage 2):
    1. Explicit `url` arg
    2. TRAIDO_JOURNAL_DATABASE_URL
    3. DATABASE_URL (Railway / Compose inject this for Postgres)
    4. Local SQLite file (default — no Docker/Postgres required)
    """
    raw = url or os.getenv("TRAIDO_JOURNAL_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not raw:
        return _default_sqlite_url()
    if raw.startswith("postgresql+asyncpg://"):
        return raw.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if raw.startswith("postgresql://"):
        return raw.replace("postgresql://", "postgresql+psycopg://", 1)
    if raw.startswith("postgres://"):
        return raw.replace("postgres://", "postgresql+psycopg://", 1)
    return raw


@lru_cache
def get_sync_engine(url: str | None = None) -> Engine:
    resolved = resolve_sync_database_url(url)
    connect_args = {"check_same_thread": False} if resolved.startswith("sqlite") else {}
    return create_engine(resolved, future=True, connect_args=connect_args)


def schema_drift(engine: Engine) -> list[str]:
    """Everything the models expect that the live schema does not have.

    Columns matter as much as tables here. `create_all` adds a table it has
    never seen but never alters one it has, so a migration that adds a column
    leaves an already-created database looking complete. Nothing complains
    until a query touches the new column, and on this desk the first query to
    do so is reconciliation — which then fails one poll at a time, behind a log
    line, while the desk keeps answering 200.
    """
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    drift: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            drift.append(table.name)
            continue
        present = {col["name"] for col in inspector.get_columns(table.name)}
        drift.extend(f"{table.name}.{c.name}" for c in table.columns if c.name not in present)

        # Unique indexes only. A missing ordinary index costs speed; a missing
        # unique one costs a guarantee — the one-open-position-per-symbol rule
        # lives in an index, and a database quietly without it looks identical
        # to one that has it right up until the day two rows appear.
        indexed = {ix["name"] for ix in inspector.get_indexes(table.name)}
        drift.extend(
            f"{table.name} index {ix.name}"
            for ix in table.indexes
            if ix.unique and ix.name not in indexed
        )
    return sorted(drift)


def _create_missing_indexes(engine: Engine) -> None:
    """Add indexes to tables that already exist. SQLite dev databases only.

    `create_all` builds a table's indexes when it builds the table, and skips
    both for a table already there. So a local database created before an index
    was declared never gains it — which for a unique index means a constraint
    the code believes it has and the database has never enforced.

    On a server this is Alembic's job and doing it here would paper over a
    missing migration. Locally there is no migration history to respect, and the
    alternative is a developer whose desk enforces fewer invariants than
    production while looking identical.
    """
    inspector = inspect(engine)
    existing = set(inspector.get_table_names())
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            continue
        present = {ix["name"] for ix in inspector.get_indexes(table.name)}
        columns = {col["name"] for col in inspector.get_columns(table.name)}
        for index in table.indexes:
            if index.name in present:
                continue
            if not {c.name for c in index.columns} <= columns:
                # The column itself is missing, which is a migration this
                # function has no business performing. Left for the drift check
                # to report by name.
                continue
            try:
                index.create(engine)
            except IntegrityError as exc:
                # A unique index the existing rows violate. Never forced, and
                # never skipped either: the data is already in a shape the model
                # says is impossible, and only a human can decide which row wins.
                raise RuntimeError(
                    f"Cannot create {index.name} on {table.name}: the existing rows "
                    f"already violate it ({exc.orig}). Resolve the duplicates by hand — "
                    "see the matching Alembic revision for the procedure."
                ) from exc


def init_db(engine: Engine | None = None) -> Engine:
    """
    Prepare the journal database, and refuse to run against a stale one.

    SQLite is a local dev convenience, so creating tables in place is fine. On
    a real server the schema is owned by Alembic: `create_all` there would
    paper over a missing migration and let the app run against a schema nobody
    reviewed. Either way the result is then checked against the models, because
    a half-migrated database is not a lesser version of a migrated one — it is
    one where the code's idea of what we hold and what we ordered silently
    stops matching what is stored.
    """
    eng = engine or get_sync_engine()
    from database import models as _models  # noqa: F401

    if eng.dialect.name == "sqlite":
        Base.metadata.create_all(eng)
        _create_missing_indexes(eng)

    drift = schema_drift(eng)
    if drift:
        raise RuntimeError(
            "Journal schema does not match the models. Missing: "
            + ", ".join(drift)
            + ". Run `alembic upgrade head` before starting the API. A database "
            "created by an earlier `create_all` has no revision recorded and "
            "must be stamped at its current revision first."
        )
    return eng


_PREPARED: MutableSet[Engine] = WeakSet()
_PREPARE_LOCK = Lock()


def session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Sessions for an engine whose schema has been checked — checked once.

    Every store on the desk built its factory by calling `init_db` inline, so a
    full schema inspection ran on each individual operation: `create_all`, an
    index sweep, then `get_columns` for every table in the metadata. That is
    dozens of PRAGMA round trips per audit append and per intent read, on the
    path an order is placed through.

    Two things are wrong with it beyond the cost. `init_db` is a startup
    assertion — it refuses to run against a schema that has drifted — and
    re-asking it mid-flight means order placement can raise a migration error
    from inside a store call, where nothing is prepared to interpret one.

    And it is not thread-safe. Reflection consumes PRAGMA cursors, so two
    threads inspecting the same table over one connection tear each other's
    results apart; `test_22` — two approvals fired at once — failed roughly one
    run in ten with `IndexError` from deep inside SQLAlchemy's SQLite dialect,
    which reads as a database corruption rather than as what it is. Preparing
    once behind a lock removes the window entirely.

    What is remembered is that an engine has been prepared, not the factory
    built from it. A `sessionmaker` holds the engine it is bound to, so caching
    factories under weak keys would have each value keep its own key alive and
    the set would never release anything — one live engine, and for SQLite one
    open connection, per test in the suite. Building a `sessionmaker` is a few
    attribute assignments; the inspection is what was worth avoiding.
    """
    eng = engine or get_sync_engine()
    if eng not in _PREPARED:
        with _PREPARE_LOCK:
            if eng not in _PREPARED:
                init_db(eng)
                _PREPARED.add(eng)
    return sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)
