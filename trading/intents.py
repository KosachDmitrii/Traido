"""
Durable store for order intents.

Two implementations with one API, matching the pattern already used for
opportunities and exits: SQL for production, in-memory for unit tests.

The contract that matters:

- `create_or_get` is idempotent on `idempotency_key`. A second call with the
  same key returns the first intent and `created=False`, so the caller can see
  it must not submit again.
- `transition` validates against the *persisted* status, not a stale copy the
  caller is holding. Two workers racing on the same intent cannot both win.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from core.enums import IntentStatus
from database.models.desk import OrderIntentRow
from database.session import session_factory
from trading.ledger import ExitApplication
from trading.order_intent import UNRESOLVED, OrderIntent, assert_transition
from trading.pricing import round_equity_price, round_equity_qty


class OrderIntentStorePort(Protocol):
    def create_or_get(self, intent: OrderIntent) -> tuple[OrderIntent, bool]: ...

    def get(self, intent_id: UUID) -> OrderIntent | None: ...

    def get_by_key(self, idempotency_key: str) -> OrderIntent | None: ...

    def transition(
        self,
        intent_id: UUID,
        to_status: IntentStatus,
        **updates: Any,
    ) -> OrderIntent: ...

    def transition_from(
        self,
        intent_id: UUID,
        *,
        from_status: IntentStatus,
        to_status: IntentStatus,
        **updates: Any,
    ) -> OrderIntent | None: ...

    def update_fields(self, intent_id: UUID, **updates: Any) -> OrderIntent: ...

    def claim_exit_qty(self, intent_id: UUID, *, expect: Decimal, claim: Decimal) -> bool: ...

    def list_unresolved(self) -> list[OrderIntent]: ...

    def list_by_key_prefix(self, prefix: str) -> list[OrderIntent]: ...

    def unresolved_symbols(self) -> set[str]: ...


def _session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Prepared once per engine — see `database.session.session_factory`."""
    return session_factory(engine)


def _from_row(row: OrderIntentRow) -> OrderIntent:
    return OrderIntent.model_validate(row.payload)


def _write(session: Session, intent: OrderIntent) -> None:
    row = session.get(OrderIntentRow, intent.id)
    data = intent.model_dump(mode="json")
    if row is None:
        session.add(
            OrderIntentRow(
                id=intent.id,
                idempotency_key=intent.idempotency_key,
                purpose=intent.purpose.value,
                broker=intent.broker,
                symbol=intent.symbol,
                status=intent.status.value,
                broker_order_id=intent.broker_order_id,
                opportunity_id=intent.opportunity_id,
                position_id=intent.position_id,
                created_at=intent.created_at,
                payload=data,
            )
        )
    else:
        row.status = intent.status.value
        row.broker_order_id = intent.broker_order_id
        row.symbol = intent.symbol
        row.purpose = intent.purpose.value
        row.position_id = intent.position_id
        row.payload = data


def _apply(intent: OrderIntent, to_status: IntentStatus, updates: dict[str, Any]) -> OrderIntent:
    assert_transition(intent.status, to_status)
    return intent.model_copy(
        update={**updates, "status": to_status, "updated_at": datetime.now(UTC)}
    )


class OrderIntentStore:
    """SQL-backed store. The unique index on idempotency_key is the guard."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine
        self._lock = Lock()

    def create_or_get(self, intent: OrderIntent) -> tuple[OrderIntent, bool]:
        SessionLocal = _session_factory(self._engine)
        with self._lock, SessionLocal() as session:
            existing = self._by_key(session, intent.idempotency_key)
            if existing is not None:
                return existing, False
            _write(session, intent)
            try:
                session.commit()
            except IntegrityError:
                # Lost the race against another worker holding the same key.
                # Its intent is the winner; ours was never sent anywhere.
                session.rollback()
                found = self._by_key(session, intent.idempotency_key)
                if found is None:
                    raise
                return found, False
            return intent, True

    def get(self, intent_id: UUID) -> OrderIntent | None:
        SessionLocal = _session_factory(self._engine)
        with SessionLocal() as session:
            row = session.get(OrderIntentRow, intent_id)
            return _from_row(row) if row else None

    def get_by_key(self, idempotency_key: str) -> OrderIntent | None:
        SessionLocal = _session_factory(self._engine)
        with SessionLocal() as session:
            return self._by_key(session, idempotency_key)

    def transition(
        self,
        intent_id: UUID,
        to_status: IntentStatus,
        **updates: Any,
    ) -> OrderIntent:
        SessionLocal = _session_factory(self._engine)
        with self._lock, SessionLocal() as session:
            row = session.get(OrderIntentRow, intent_id)
            if row is None:
                raise ValueError("order_intent_not_found")
            updated = _apply(_from_row(row), to_status, updates)
            _write(session, updated)
            session.commit()
            return updated

    def transition_from(
        self,
        intent_id: UUID,
        *,
        from_status: IntentStatus,
        to_status: IntentStatus,
        **updates: Any,
    ) -> OrderIntent | None:
        """Compare-and-swap status. Loser gets None and must not submit.

        Two workers that both see CREATED both try SUBMITTING; the database
        row lock plus `WHERE status = from` admits exactly one. The loser
        recovers the winner's order rather than placing a second.
        """
        SessionLocal = _session_factory(self._engine)
        with self._lock, SessionLocal() as session:
            row = (
                session.query(OrderIntentRow)
                .filter(
                    OrderIntentRow.id == intent_id,
                    OrderIntentRow.status == from_status.value,
                )
                .with_for_update()
                .one_or_none()
            )
            if row is None:
                return None
            updated = _apply(_from_row(row), to_status, updates)
            _write(session, updated)
            session.commit()
            return updated

    def update_fields(self, intent_id: UUID, **updates: Any) -> OrderIntent:
        """Record new broker information without asserting a state change."""
        SessionLocal = _session_factory(self._engine)
        with self._lock, SessionLocal() as session:
            row = session.get(OrderIntentRow, intent_id)
            if row is None:
                raise ValueError("order_intent_not_found")
            updated = _from_row(row).model_copy(update={**updates, "updated_at": datetime.now(UTC)})
            _write(session, updated)
            session.commit()
            return updated

    def claim_exit_qty(self, intent_id: UUID, *, expect: Decimal, claim: Decimal) -> bool:
        """Reserve the right to reduce the position by `claim - expect` shares.

        Returns True to exactly one caller per value of `expect`. The read, the
        comparison and the write happen inside one transaction with the row
        locked, so a second reader of the same fill finds the quantity already
        absorbed and is told no.

        On PostgreSQL `with_for_update` is a real row lock. On SQLite it is a
        no-op, but SQLite serialises write transactions at the database level,
        which gives the same outcome for the single-writer deployment the desk
        runs under.
        """
        SessionLocal = _session_factory(self._engine)
        with self._lock, SessionLocal() as session:
            row = (
                session.query(OrderIntentRow)
                .filter(OrderIntentRow.id == intent_id)
                .with_for_update()
                .one_or_none()
            )
            if row is None:
                raise ValueError("order_intent_not_found")
            current = _from_row(row)
            if current.applied_exit_qty != expect:
                session.rollback()
                return False
            _write(
                session,
                current.model_copy(
                    update={"applied_exit_qty": claim, "updated_at": datetime.now(UTC)}
                ),
            )
            session.commit()
            return True

    def list_unresolved(self) -> list[OrderIntent]:
        SessionLocal = _session_factory(self._engine)
        with SessionLocal() as session:
            rows = (
                session.query(OrderIntentRow)
                .filter(OrderIntentRow.status.in_([s.value for s in UNRESOLVED]))
                .order_by(OrderIntentRow.created_at.asc())
                .all()
            )
            return [_from_row(r) for r in rows]

    def list_by_key_prefix(self, prefix: str) -> list[OrderIntent]:
        SessionLocal = _session_factory(self._engine)
        with SessionLocal() as session:
            rows = (
                session.query(OrderIntentRow)
                .filter(OrderIntentRow.idempotency_key.startswith(prefix))
                .order_by(OrderIntentRow.created_at.asc())
                .all()
            )
            return [_from_row(r) for r in rows]

    def unresolved_symbols(self) -> set[str]:
        return {i.symbol.upper() for i in self.list_unresolved() if i.blocks_new_exposure}

    @staticmethod
    def _by_key(session: Session, idempotency_key: str) -> OrderIntent | None:
        row = (
            session.query(OrderIntentRow)
            .filter(OrderIntentRow.idempotency_key == idempotency_key)
            .one_or_none()
        )
        return _from_row(row) if row else None


class MemoryOrderIntentStore:
    """In-memory twin for unit tests. Same guarantees, no database."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._items: dict[UUID, OrderIntent] = {}
        self._keys: dict[str, UUID] = {}

    def create_or_get(self, intent: OrderIntent) -> tuple[OrderIntent, bool]:
        with self._lock:
            existing_id = self._keys.get(intent.idempotency_key)
            if existing_id is not None:
                return self._items[existing_id], False
            self._items[intent.id] = intent
            self._keys[intent.idempotency_key] = intent.id
            return intent, True

    def get(self, intent_id: UUID) -> OrderIntent | None:
        with self._lock:
            return self._items.get(intent_id)

    def get_by_key(self, idempotency_key: str) -> OrderIntent | None:
        with self._lock:
            found = self._keys.get(idempotency_key)
            return self._items.get(found) if found else None

    def transition(
        self,
        intent_id: UUID,
        to_status: IntentStatus,
        **updates: Any,
    ) -> OrderIntent:
        with self._lock:
            current = self._items.get(intent_id)
            if current is None:
                raise ValueError("order_intent_not_found")
            updated = _apply(current, to_status, updates)
            self._items[intent_id] = updated
            return updated

    def transition_from(
        self,
        intent_id: UUID,
        *,
        from_status: IntentStatus,
        to_status: IntentStatus,
        **updates: Any,
    ) -> OrderIntent | None:
        with self._lock:
            current = self._items.get(intent_id)
            if current is None or current.status is not from_status:
                return None
            updated = _apply(current, to_status, updates)
            self._items[intent_id] = updated
            return updated

    def update_fields(self, intent_id: UUID, **updates: Any) -> OrderIntent:
        with self._lock:
            current = self._items.get(intent_id)
            if current is None:
                raise ValueError("order_intent_not_found")
            updated = current.model_copy(update={**updates, "updated_at": datetime.now(UTC)})
            self._items[intent_id] = updated
            return updated

    def claim_exit_qty(self, intent_id: UUID, *, expect: Decimal, claim: Decimal) -> bool:
        with self._lock:
            current = self._items.get(intent_id)
            if current is None:
                raise ValueError("order_intent_not_found")
            if current.applied_exit_qty != expect:
                return False
            self._items[intent_id] = current.model_copy(
                update={"applied_exit_qty": claim, "updated_at": datetime.now(UTC)}
            )
            return True

    def list_unresolved(self) -> list[OrderIntent]:
        with self._lock:
            return sorted(
                (i for i in self._items.values() if i.status in UNRESOLVED),
                key=lambda i: i.created_at,
            )

    def list_by_key_prefix(self, prefix: str) -> list[OrderIntent]:
        with self._lock:
            return sorted(
                (i for i in self._items.values() if i.idempotency_key.startswith(prefix)),
                key=lambda i: i.created_at,
            )

    def unresolved_symbols(self) -> set[str]:
        return {i.symbol.upper() for i in self.list_unresolved() if i.blocks_new_exposure}


INTENTS: OrderIntentStore = OrderIntentStore()


def apply_exit_to_ledger(
    store: OrderIntentStorePort,
    intent: OrderIntent,
    *,
    filled_qty: Decimal,
    exit_price: Decimal,
    reasons: list[str],
) -> ExitApplication:
    """Reduce the position by the part of this exit the book has not yet absorbed.

    Both the live exit path and reconciliation call this, and they can easily
    see the same fill: the service applies it, then a reconciliation pass reads
    the same filled order off the broker. Tracking absorbed quantity on the
    intent is what stops the second reader from selling the position twice on
    paper.
    """
    from trading.ledger import LEDGER

    already = intent.applied_exit_qty
    delta = round_equity_qty(filled_qty) - already
    unchanged = ExitApplication(
        position_id=intent.position_id,
        closed=False,
        filled_qty=Decimal(0),
        remaining_qty=_remaining_qty(intent),
    )
    if delta <= 0:
        return unchanged

    # The claim comes first, and that ordering is the whole fix (P0-3).
    #
    # Reducing the book and then recording how much was absorbed is two
    # transactions with a window between them. A crash in that window — or a
    # reconciliation pass reading the same filled order — reduces the position
    # twice for one fill, which closes it on paper while real shares are still
    # held, and journals a trade that never happened. Nothing downstream
    # detects that: a book saying "flat" produces no mismatch to investigate,
    # and the shares sit unprotected.
    #
    # Claiming first inverts the failure. The window now loses a reduction
    # rather than duplicating one, leaving the book larger than the venue —
    # which `reconcile_position_quantities` does detect, refuses to absorb, and
    # blocks the symbol over, while the excess-protection sweep keeps the stop
    # from covering shares that are gone. Both outcomes are bad; only one of
    # them is silent.
    if not store.claim_exit_qty(intent.id, expect=already, claim=already + delta):
        return unchanged

    return LEDGER.apply_exit_fill(
        symbol=intent.symbol,
        position_id=intent.position_id,
        filled_qty=delta,
        exit_price=round_equity_price(exit_price),
        exit_reasons=reasons,
    )


def _remaining_qty(intent: OrderIntent) -> Decimal:
    from trading.ledger import LEDGER

    row = (
        LEDGER.get(intent.position_id)
        if intent.position_id
        else LEDGER.find_open_by_symbol(intent.symbol)
    )
    return Decimal(str(row.qty)) if row is not None and row.status == "open" else Decimal(0)
