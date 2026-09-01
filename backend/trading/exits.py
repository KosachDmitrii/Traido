"""Exit proposal store (SQL) + basic position monitoring."""

from __future__ import annotations

from datetime import UTC, datetime
from threading import Lock
from uuid import UUID, uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.ports import BrokerPort, MarketDataPort
from core.schemas import ExitProposal, StrictModel
from database.models.desk import ExitOpportunityRow
from database.session import session_factory

EXIT_AWAITING = "awaiting_confirmation"
EXIT_APPROVING = "approving"
EXIT_SOLD = "sold"
EXIT_HELD = "held"
EXIT_EXPIRED = "expired"

OPERATOR_CLOSE_REASON = "Closed on operator request"
"""Marks a card the operator raised rather than an agent.

The position agent withdraws proposals whose reason has stopped holding, and it
does not know this one: a discretionary close has no rule behind it to stop
being true. Without the marker the background pass could expire the card between
`close_position` writing it and `decide_exit` claiming it, and the operator's
click would come back as `invalid_status` for no reason they could see.
"""


class ExitOpportunity(StrictModel):
    id: UUID
    proposal: ExitProposal
    status: str = EXIT_AWAITING
    created_at: datetime


def _session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Prepared once per engine — see `database.session.session_factory`."""
    return session_factory(engine)


def _from_row(row: ExitOpportunityRow) -> ExitOpportunity:
    return ExitOpportunity.model_validate(row.payload)


def _write_payload(session: Session, item: ExitOpportunity) -> ExitOpportunityRow:
    row = session.get(ExitOpportunityRow, item.id)
    data = item.model_dump(mode="json")
    if row is None:
        row = ExitOpportunityRow(
            id=item.id,
            status=item.status,
            symbol=item.proposal.symbol,
            position_id=item.proposal.position_id,
            created_at=item.created_at,
            payload=data,
        )
        session.add(row)
    else:
        row.status = item.status
        row.symbol = item.proposal.symbol
        row.position_id = item.proposal.position_id
        row.created_at = item.created_at
        row.payload = data
    return row


class ExitStore:
    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine
        self._lock = Lock()

    def upsert(self, proposal: ExitProposal) -> ExitOpportunity:
        SessionLocal = _session_factory(self._engine)
        with self._lock, SessionLocal() as session:
            rows = (
                session.query(ExitOpportunityRow)
                .filter(
                    ExitOpportunityRow.status == EXIT_AWAITING,
                    ExitOpportunityRow.symbol == proposal.symbol,
                    ExitOpportunityRow.position_id == proposal.position_id,
                )
                .all()
            )
            if rows:
                item = _from_row(rows[0]).model_copy(update={"proposal": proposal})
                _write_payload(session, item)
                session.commit()
                return item
            item = ExitOpportunity(
                id=uuid4(),
                proposal=proposal,
                created_at=datetime.now(UTC),
            )
            _write_payload(session, item)
            session.commit()
            return item

    def get(self, exit_id: UUID) -> ExitOpportunity | None:
        SessionLocal = _session_factory(self._engine)
        with SessionLocal() as session:
            row = session.get(ExitOpportunityRow, exit_id)
            return _from_row(row) if row else None

    def update(self, item: ExitOpportunity) -> ExitOpportunity:
        SessionLocal = _session_factory(self._engine)
        with self._lock, SessionLocal() as session:
            _write_payload(session, item)
            session.commit()
        return item

    def claim(
        self,
        exit_id: UUID,
        *,
        from_status: str,
        to_status: str,
    ) -> ExitOpportunity | None:
        SessionLocal = _session_factory(self._engine)
        with self._lock, SessionLocal() as session:
            row = session.get(ExitOpportunityRow, exit_id)
            if row is None or row.status != from_status:
                return None
            item = _from_row(row).model_copy(update={"status": to_status})
            _write_payload(session, item)
            session.commit()
            return item

    def list_open(self) -> list[ExitOpportunity]:
        SessionLocal = _session_factory(self._engine)
        with SessionLocal() as session:
            rows = (
                session.query(ExitOpportunityRow)
                .filter(ExitOpportunityRow.status == EXIT_AWAITING)
                .order_by(ExitOpportunityRow.created_at.desc())
                .all()
            )
            return [_from_row(r) for r in rows]


class MemoryExitStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._items: dict[UUID, ExitOpportunity] = {}

    def upsert(self, proposal: ExitProposal) -> ExitOpportunity:
        with self._lock:
            for item in self._items.values():
                if (
                    item.status == EXIT_AWAITING
                    and item.proposal.symbol == proposal.symbol
                    and item.proposal.position_id == proposal.position_id
                ):
                    item = item.model_copy(update={"proposal": proposal})
                    self._items[item.id] = item
                    return item
            opp = ExitOpportunity(
                id=uuid4(),
                proposal=proposal,
                created_at=datetime.now(UTC),
            )
            self._items[opp.id] = opp
            return opp

    def get(self, exit_id: UUID) -> ExitOpportunity | None:
        with self._lock:
            return self._items.get(exit_id)

    def update(self, item: ExitOpportunity) -> ExitOpportunity:
        with self._lock:
            self._items[item.id] = item
        return item

    def claim(
        self,
        exit_id: UUID,
        *,
        from_status: str,
        to_status: str,
    ) -> ExitOpportunity | None:
        with self._lock:
            item = self._items.get(exit_id)
            if item is None or item.status != from_status:
                return None
            updated = item.model_copy(update={"status": to_status})
            self._items[exit_id] = updated
            return updated

    def list_open(self) -> list[ExitOpportunity]:
        with self._lock:
            return [i for i in self._items.values() if i.status == EXIT_AWAITING]


EXITS = ExitStore()


async def refresh_exit_proposals(
    broker: BrokerPort, market_data: MarketDataPort
) -> list[ExitOpportunity]:
    """Delegate to Position Agent (Stage 5)."""
    from agents.position.agent import assess_exits

    return await assess_exits(broker, market_data)
