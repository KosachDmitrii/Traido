"""SQL-backed opportunity store with compare-and-swap claims."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.activity import BOARD
from core.enums import OpportunityStatus, TradingMode
from core.schemas import RiskDecision, TradeCandidate, TradeOpportunity
from database.models.desk import OpportunityRow
from database.session import session_factory


def _session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Prepared once per engine — see `database.session.session_factory`."""
    return session_factory(engine)


def _from_row(row: OpportunityRow) -> TradeOpportunity:
    return TradeOpportunity.model_validate(row.payload)


def _write_payload(session: Session, opp: TradeOpportunity) -> OpportunityRow:
    row = session.get(OpportunityRow, opp.id)
    data = opp.model_dump(mode="json")
    if row is None:
        row = OpportunityRow(
            id=opp.id,
            status=opp.status.value,
            trading_mode=opp.trading_mode.value,
            symbol=opp.candidate.symbol,
            created_at=opp.created_at,
            expires_at=opp.expires_at,
            payload=data,
        )
        session.add(row)
    else:
        row.status = opp.status.value
        row.trading_mode = opp.trading_mode.value
        row.symbol = opp.candidate.symbol
        row.created_at = opp.created_at
        row.expires_at = opp.expires_at
        row.payload = data
    return row


class OpportunityStore:
    """Default Stage 4 store — SQLite/Postgres via journal engine."""

    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine
        self._lock = Lock()

    def create(
        self,
        candidate: TradeCandidate,
        risk: RiskDecision,
        trading_mode: TradingMode,
        *,
        ttl_minutes: int = 60,
    ) -> TradeOpportunity:
        now = datetime.now(UTC)
        opp = TradeOpportunity(
            id=uuid4(),
            candidate=candidate,
            risk=risk,
            status=OpportunityStatus.AWAITING_CONFIRMATION,
            trading_mode=trading_mode,
            created_at=now,
            expires_at=now + timedelta(minutes=ttl_minutes),
            proposed_qty=risk.sized_qty,
            signal_detected_at=now,
            signal_price=candidate.signal_price or candidate.entry,
            published_at=now,
            published_price=candidate.entry,
        )
        SessionLocal = _session_factory(self._engine)
        with self._lock, SessionLocal() as session:
            _write_payload(session, opp)
            session.commit()
        return opp

    def get(self, opportunity_id: UUID) -> TradeOpportunity | None:
        SessionLocal = _session_factory(self._engine)
        # Same lock as claim/update: SQLite + concurrent TestClient threads
        # otherwise race the row processors mid-write.
        with self._lock, SessionLocal() as session:
            row = session.get(OpportunityRow, opportunity_id)
            return _from_row(row) if row else None

    def update(self, opp: TradeOpportunity) -> TradeOpportunity:
        SessionLocal = _session_factory(self._engine)
        with self._lock, SessionLocal() as session:
            _write_payload(session, opp)
            session.commit()
        return opp

    def claim(
        self,
        opportunity_id: UUID,
        *,
        from_status: OpportunityStatus,
        to_status: OpportunityStatus,
    ) -> TradeOpportunity | None:
        """Compare-and-swap status transition.

        The process lock is not the authority — `WHERE status = from` plus
        `FOR UPDATE` is. Two workers that both read AWAITING both try the
        update; exactly one sees a matching row. Returns None to the loser.
        """
        SessionLocal = _session_factory(self._engine)
        with self._lock, SessionLocal() as session:
            row = (
                session.query(OpportunityRow)
                .filter(
                    OpportunityRow.id == opportunity_id,
                    OpportunityRow.status == from_status.value,
                )
                .with_for_update()
                .one_or_none()
            )
            if row is None:
                return None
            updates: dict[str, Any] = {"status": to_status}
            if to_status == OpportunityStatus.APPROVING:
                updates["claimed_at"] = datetime.now(UTC)
            elif from_status == OpportunityStatus.APPROVING:
                updates["claimed_at"] = None
            opp = _from_row(row).model_copy(update=updates)
            _write_payload(session, opp)
            session.commit()
            return opp

    def list_open(self) -> list[TradeOpportunity]:
        SessionLocal = _session_factory(self._engine)
        with SessionLocal() as session:
            rows = (
                session.query(OpportunityRow)
                .filter(OpportunityRow.status == OpportunityStatus.AWAITING_CONFIRMATION.value)
                .order_by(OpportunityRow.created_at.desc())
                .all()
            )
            return [_from_row(r) for r in rows]

    def has_status(self, status: OpportunityStatus) -> bool:
        SessionLocal = _session_factory(self._engine)
        with SessionLocal() as session:
            row = (
                session.query(OpportunityRow.id)
                .filter(OpportunityRow.status == status.value)
                .limit(1)
                .first()
            )
            return row is not None

    def release_stale_approving(self, *, older_than_sec: float = 90.0) -> int:
        """Return cards stuck in APPROVING after proxy/reload death mid-BUY."""
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_sec)
        SessionLocal = _session_factory(self._engine)
        released = 0
        with self._lock, SessionLocal() as session:
            rows = (
                session.query(OpportunityRow)
                .filter(OpportunityRow.status == OpportunityStatus.APPROVING.value)
                .all()
            )
            for row in rows:
                opp = _from_row(row)
                claimed = opp.claimed_at or opp.created_at
                if claimed.tzinfo is None:
                    claimed = claimed.replace(tzinfo=UTC)
                # Legacy stuck rows (no claimed_at) → always release
                if opp.claimed_at is not None and claimed > cutoff:
                    continue
                opp = opp.model_copy(
                    update={
                        "status": OpportunityStatus.AWAITING_CONFIRMATION,
                        "claimed_at": None,
                    }
                )
                _write_payload(session, opp)
                released += 1
            if released:
                session.commit()
        return released


class MemoryOpportunityStore:
    """In-memory store for unit tests (same claim API)."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._items: dict[UUID, TradeOpportunity] = {}

    def create(
        self,
        candidate: TradeCandidate,
        risk: RiskDecision,
        trading_mode: TradingMode,
        *,
        ttl_minutes: int = 60,
    ) -> TradeOpportunity:
        now = datetime.now(UTC)
        opp = TradeOpportunity(
            id=uuid4(),
            candidate=candidate,
            risk=risk,
            status=OpportunityStatus.AWAITING_CONFIRMATION,
            trading_mode=trading_mode,
            created_at=now,
            expires_at=now + timedelta(minutes=ttl_minutes),
            proposed_qty=risk.sized_qty,
            signal_detected_at=now,
            signal_price=candidate.signal_price or candidate.entry,
            published_at=now,
            published_price=candidate.entry,
        )
        with self._lock:
            self._items[opp.id] = opp
        return opp

    def get(self, opportunity_id: UUID) -> TradeOpportunity | None:
        with self._lock:
            return self._items.get(opportunity_id)

    def update(self, opp: TradeOpportunity) -> TradeOpportunity:
        with self._lock:
            self._items[opp.id] = opp
        return opp

    def claim(
        self,
        opportunity_id: UUID,
        *,
        from_status: OpportunityStatus,
        to_status: OpportunityStatus,
    ) -> TradeOpportunity | None:
        with self._lock:
            opp = self._items.get(opportunity_id)
            if opp is None or opp.status != from_status:
                return None
            updates: dict[str, Any] = {"status": to_status}
            if to_status == OpportunityStatus.APPROVING:
                updates["claimed_at"] = datetime.now(UTC)
            elif from_status == OpportunityStatus.APPROVING:
                updates["claimed_at"] = None
            updated = opp.model_copy(update=updates)
            self._items[opportunity_id] = updated
            return updated

    def list_open(self) -> list[TradeOpportunity]:
        with self._lock:
            return [
                o
                for o in self._items.values()
                if o.status == OpportunityStatus.AWAITING_CONFIRMATION
            ]

    def has_status(self, status: OpportunityStatus) -> bool:
        with self._lock:
            return any(o.status == status for o in self._items.values())

    def release_stale_approving(self, *, older_than_sec: float = 90.0) -> int:
        cutoff = datetime.now(UTC) - timedelta(seconds=older_than_sec)
        released = 0
        with self._lock:
            for oid, opp in list(self._items.items()):
                if opp.status != OpportunityStatus.APPROVING:
                    continue
                claimed = opp.claimed_at or opp.created_at
                if claimed.tzinfo is None:
                    claimed = claimed.replace(tzinfo=UTC)
                if opp.claimed_at is not None and claimed > cutoff:
                    continue
                self._items[oid] = opp.model_copy(
                    update={
                        "status": OpportunityStatus.AWAITING_CONFIRMATION,
                        "claimed_at": None,
                    }
                )
                released += 1
        return released


OPPORTUNITIES = OpportunityStore()


def withdraw_unactionable(store: Any = None) -> int:
    """Take down BUY cards that can no longer become trades.

    A proposal is a standing offer to act, and until now nothing ever retracted
    one. The hour-long TTL was read only inside `decide`, so an expired card
    kept its buttons until somebody pressed them and was told it had expired;
    and a symbol that gained a position after its card was written kept offering
    an entry that `POSITION_ALREADY_OPEN` was certain to refuse. Three of the
    five queue slots were held that way, and at five the scanner stops looking
    for real ideas altogether — dead cards were crowding out live ones.

    Only durable facts are swept. A wide spread, a moved price or a closed
    session all refuse an entry too, and all of them come back: withdrawing on
    those would delete a good setup because it was briefly unbuyable. Time that
    has passed and a position that is now held do not come back.

    Every transition goes through `claim`, so a card the operator has already
    pressed is losing this race by design — it is `APPROVING` by then and this
    pass leaves it where it is rather than pulling it out from under them.
    """
    from trading.ledger import LEDGER

    target = store if store is not None else OPPORTUNITIES
    now = datetime.now(UTC)
    withdrawn = 0
    for opp in target.list_open():
        symbol = opp.candidate.symbol.upper()
        expires = opp.expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires is not None and now > expires:
            to_status = OpportunityStatus.EXPIRED
            why = "proposal is past its hour"
        elif LEDGER.find_open_by_symbol(symbol) is not None:
            to_status = OpportunityStatus.DISCARDED
            why = "position already open in this symbol"
        else:
            continue
        if target.claim(
            opp.id,
            from_status=OpportunityStatus.AWAITING_CONFIRMATION,
            to_status=to_status,
        ):
            withdrawn += 1
            BOARD.log("scanner", f"BUY proposal withdrawn · {why}", symbol=symbol)
    return withdrawn
