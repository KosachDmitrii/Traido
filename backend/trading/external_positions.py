"""External / orphan broker positions — never Traido-admitted trades.

An unattributed venue position blocks the symbol and invalidates cards.
It must not create OrderIntent, AdmissionRecord, or Opportunity attribution
without proven broker correlation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from typing import Protocol
from uuid import UUID, uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.enums import EntryWatchStatus, OpportunityStatus
from core.metrics import METRICS
from core.schemas import ExternalPositionIncident
from database.models.desk import ExternalPositionIncidentRow
from database.session import session_factory


class ExternalPositionStorePort(Protocol):
    def upsert_orphan(
        self,
        *,
        symbol: str,
        qty: Decimal,
        broker: str,
        avg_entry: Decimal | None = None,
        account_id: str | None = None,
        broker_order_id: str | None = None,
        broker_perm_id: str | None = None,
        client_order_id: str | None = None,
        notes: list[str] | None = None,
    ) -> ExternalPositionIncident: ...

    def get_open_for_symbol(self, symbol: str) -> ExternalPositionIncident | None: ...

    def list_open(self) -> list[ExternalPositionIncident]: ...

    def resolve(
        self,
        incident_id: UUID,
        *,
        resolution: str,
        operator_action: str | None = None,
    ) -> ExternalPositionIncident: ...

    def blocking_symbols(self) -> set[str]: ...


def _sf(engine: Engine | None = None) -> sessionmaker[Session]:
    return session_factory(engine)


def _from_row(row: ExternalPositionIncidentRow) -> ExternalPositionIncident:
    return ExternalPositionIncident.model_validate(row.payload)


class ExternalPositionStore:
    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine
        self._lock = Lock()

    def upsert_orphan(
        self,
        *,
        symbol: str,
        qty: Decimal,
        broker: str,
        avg_entry: Decimal | None = None,
        account_id: str | None = None,
        broker_order_id: str | None = None,
        broker_perm_id: str | None = None,
        client_order_id: str | None = None,
        notes: list[str] | None = None,
    ) -> ExternalPositionIncident:
        ticker = symbol.upper()
        now = datetime.now(UTC)
        with self._lock:
            SessionLocal = _sf(self._engine)
            with SessionLocal() as session:
                existing = (
                    session.query(ExternalPositionIncidentRow)
                    .filter(
                        ExternalPositionIncidentRow.symbol == ticker,
                        ExternalPositionIncidentRow.resolution == "open",
                    )
                    .one_or_none()
                )
                if existing is not None:
                    inc = _from_row(existing)
                    updated = inc.model_copy(
                        update={
                            "qty": qty,
                            "avg_entry": avg_entry if avg_entry is not None else inc.avg_entry,
                            "last_seen_at": now,
                            "updated_at": now,
                            "broker_order_id": broker_order_id or inc.broker_order_id,
                            "broker_perm_id": broker_perm_id or inc.broker_perm_id,
                            "client_order_id": client_order_id or inc.client_order_id,
                            "notes": list(dict.fromkeys([*inc.notes, *(notes or [])])),
                        }
                    )
                    existing.payload = updated.model_dump(mode="json")
                    existing.last_seen_at = now
                    existing.qty = str(qty)
                    session.commit()
                    return updated

                inc = ExternalPositionIncident(
                    id=uuid4(),
                    account_id=account_id,
                    broker=broker,
                    symbol=ticker,
                    qty=qty,
                    avg_entry=avg_entry,
                    first_seen_at=now,
                    last_seen_at=now,
                    broker_order_id=broker_order_id,
                    broker_perm_id=broker_perm_id,
                    client_order_id=client_order_id,
                    correlation_status="unattributed",
                    resolution="open",
                    blocks_symbol=True,
                    notes=list(notes or ["orphan_broker_position"]),
                    created_at=now,
                    updated_at=now,
                )
                session.add(
                    ExternalPositionIncidentRow(
                        id=inc.id,
                        symbol=ticker,
                        broker=broker,
                        qty=str(qty),
                        resolution="open",
                        first_seen_at=now,
                        last_seen_at=now,
                        payload=inc.model_dump(mode="json"),
                    )
                )
                session.commit()
                METRICS.counter(
                    "unattributed_fill",
                    help_text="Orphan/external broker position without Traido correlation",
                )
                return inc

    def get_open_for_symbol(self, symbol: str) -> ExternalPositionIncident | None:
        SessionLocal = _sf(self._engine)
        with SessionLocal() as session:
            row = (
                session.query(ExternalPositionIncidentRow)
                .filter(
                    ExternalPositionIncidentRow.symbol == symbol.upper(),
                    ExternalPositionIncidentRow.resolution == "open",
                )
                .one_or_none()
            )
            return _from_row(row) if row else None

    def list_open(self) -> list[ExternalPositionIncident]:
        SessionLocal = _sf(self._engine)
        with SessionLocal() as session:
            rows = (
                session.query(ExternalPositionIncidentRow)
                .filter(ExternalPositionIncidentRow.resolution == "open")
                .all()
            )
            return [_from_row(r) for r in rows]

    def resolve(
        self,
        incident_id: UUID,
        *,
        resolution: str,
        operator_action: str | None = None,
    ) -> ExternalPositionIncident:
        now = datetime.now(UTC)
        with self._lock:
            SessionLocal = _sf(self._engine)
            with SessionLocal() as session:
                row = session.get(ExternalPositionIncidentRow, incident_id)
                if row is None:
                    raise ValueError("external_position_incident_not_found")
                inc = _from_row(row).model_copy(
                    update={
                        "resolution": resolution,
                        "operator_action": operator_action,
                        "blocks_symbol": False,
                        "updated_at": now,
                    }
                )
                row.resolution = resolution
                row.payload = inc.model_dump(mode="json")
                session.commit()
                return inc

    def blocking_symbols(self) -> set[str]:
        return {i.symbol.upper() for i in self.list_open() if i.blocks_symbol}


class MemoryExternalPositionStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._items: dict[UUID, ExternalPositionIncident] = {}

    def upsert_orphan(
        self,
        *,
        symbol: str,
        qty: Decimal,
        broker: str,
        avg_entry: Decimal | None = None,
        account_id: str | None = None,
        broker_order_id: str | None = None,
        broker_perm_id: str | None = None,
        client_order_id: str | None = None,
        notes: list[str] | None = None,
    ) -> ExternalPositionIncident:
        ticker = symbol.upper()
        now = datetime.now(UTC)
        with self._lock:
            for inc in self._items.values():
                if inc.symbol == ticker and inc.resolution == "open":
                    updated = inc.model_copy(
                        update={
                            "qty": qty,
                            "avg_entry": avg_entry if avg_entry is not None else inc.avg_entry,
                            "last_seen_at": now,
                            "updated_at": now,
                            "notes": list(dict.fromkeys([*inc.notes, *(notes or [])])),
                        }
                    )
                    self._items[inc.id] = updated
                    return updated
            inc = ExternalPositionIncident(
                id=uuid4(),
                account_id=account_id,
                broker=broker,
                symbol=ticker,
                qty=qty,
                avg_entry=avg_entry,
                first_seen_at=now,
                last_seen_at=now,
                broker_order_id=broker_order_id,
                broker_perm_id=broker_perm_id,
                client_order_id=client_order_id,
                correlation_status="unattributed",
                resolution="open",
                blocks_symbol=True,
                notes=list(notes or ["orphan_broker_position"]),
                created_at=now,
                updated_at=now,
            )
            self._items[inc.id] = inc
            METRICS.counter(
                "unattributed_fill",
                help_text="Orphan/external broker position without Traido correlation",
            )
            return inc

    def get_open_for_symbol(self, symbol: str) -> ExternalPositionIncident | None:
        with self._lock:
            for inc in self._items.values():
                if inc.symbol == symbol.upper() and inc.resolution == "open":
                    return inc
            return None

    def list_open(self) -> list[ExternalPositionIncident]:
        with self._lock:
            return [i for i in self._items.values() if i.resolution == "open"]

    def resolve(
        self,
        incident_id: UUID,
        *,
        resolution: str,
        operator_action: str | None = None,
    ) -> ExternalPositionIncident:
        with self._lock:
            inc = self._items.get(incident_id)
            if inc is None:
                raise ValueError("external_position_incident_not_found")
            updated = inc.model_copy(
                update={
                    "resolution": resolution,
                    "operator_action": operator_action,
                    "blocks_symbol": False,
                    "updated_at": datetime.now(UTC),
                }
            )
            self._items[incident_id] = updated
            return updated

    def blocking_symbols(self) -> set[str]:
        return {i.symbol.upper() for i in self.list_open() if i.blocks_symbol}


EXTERNAL_POSITIONS: ExternalPositionStorePort = ExternalPositionStore()


def invalidate_symbol_for_orphan(symbol: str) -> dict[str, int]:
    """Invalidate opportunities and watches for an orphaned symbol. No admission."""
    from trading.entry_watches import ENTRY_WATCHES
    from trading.opportunities import OPPORTUNITIES

    ticker = symbol.upper()
    invalidated_opps = 0
    invalidated_watches = 0

    for opp in list(OPPORTUNITIES.list_open()):
        if opp.candidate.symbol.upper() != ticker:
            continue
        if opp.status is OpportunityStatus.AWAITING_CONFIRMATION:
            OPPORTUNITIES.claim(
                opp.id,
                from_status=OpportunityStatus.AWAITING_CONFIRMATION,
                to_status=OpportunityStatus.EXPIRED,
            )
            invalidated_opps += 1

    # Memory/SQL watch stores expose list_active differently; best-effort.
    list_fn = getattr(ENTRY_WATCHES, "list_active", None) or getattr(
        ENTRY_WATCHES, "list_open", None
    )
    if callable(list_fn):
        for watch in list(list_fn()):
            if getattr(watch, "symbol", "").upper() != ticker:
                continue
            mark = getattr(ENTRY_WATCHES, "mark", None)
            if callable(mark):
                mark(watch.id, EntryWatchStatus.INVALIDATED, reason="orphan_external_position")
                invalidated_watches += 1

    return {"opportunities": invalidated_opps, "watches": invalidated_watches}
