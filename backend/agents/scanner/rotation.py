"""Persist selection history; rotate budget slots, never eligibility gates."""

from datetime import UTC, datetime, time
from typing import Protocol

from sqlalchemy import select

from core.clock import ET, market_date
from database.models.desk import AuditEventRow
from database.session import session_factory


class Symbolic(Protocol):
    symbol: str


EVENT = "ScannerSelectionV1"


def fair_order[T: Symbolic](
    ranked: list[T], limit: int, last_seen: dict[str, float] | None
) -> list[T]:
    """Reserve half the budget for least-recently selected eligible names.

    The other half retains ranking. Equal ages retain the incoming ranking.
    First observation therefore leaves the original selection unchanged.
    """
    if not last_seen or limit <= 0 or len(ranked) <= limit:
        return ranked
    core_size = limit // 2
    core = ranked[:core_size]
    tail = sorted(ranked[core_size:], key=lambda item: last_seen.get(item.symbol, 0))
    return [*core, *tail]


def load_history() -> dict[str, dict[str, float]]:
    """Reconstruct this exchange day's selections, including after restart."""
    start = datetime.combine(market_date(), time.min, tzinfo=ET).astimezone(UTC)
    history: dict[str, dict[str, float]] = {}
    with session_factory()() as session:
        rows = session.scalars(
            select(AuditEventRow)
            .where(AuditEventRow.event_type == EVENT, AuditEventRow.created_at >= start)
            .order_by(AuditEventRow.created_at)
        )
        for row in rows:
            stage = str(row.payload["stage"])
            stamp = (
                row.created_at.replace(tzinfo=UTC)
                if row.created_at.tzinfo is None
                else row.created_at
            )
            for symbol in row.payload["symbols"]:
                history.setdefault(stage, {})[symbol] = stamp.timestamp()
    return history


def record_selection(stage: str, symbols: list[str]) -> None:
    """Persist scheduled work, not a claim that analysis or a trade succeeded."""
    if not symbols:
        return
    with session_factory()() as session:
        session.add(
            AuditEventRow(
                event_type=EVENT,
                actor="scanner",
                created_at=datetime.now(UTC),
                payload={"stage": stage, "symbols": symbols, "policy": "rank-half-lru-half-v1"},
            )
        )
        session.commit()
