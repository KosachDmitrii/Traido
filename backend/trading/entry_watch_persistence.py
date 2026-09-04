"""SQL persistence for EntryWatch — survives process restart."""

from __future__ import annotations

import logging
from threading import Lock
from uuid import UUID

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from core.enums import EntryWatchStatus
from core.schemas import EntryDecisionBundle, EntryWatch, TradeCandidate
from database.models.desk import EntryWatchRow
from database.session import session_factory
from trading.entry_watches import ENTRY_WATCHES, EntryWatchStore

logger = logging.getLogger(__name__)

_ACTIONABLE = {
    EntryWatchStatus.WAITING,
    EntryWatchStatus.TRIGGERED,
    EntryWatchStatus.REVALIDATING,
    EntryWatchStatus.ADMITTED,
    EntryWatchStatus.CONVERTING,
    EntryWatchStatus.BLOCKED_DATA,
    EntryWatchStatus.BLOCKED_OPERATIONAL,
}
_enabled = False
_lock = Lock()


def configure_entry_watch_persistence(*, enabled: bool = True) -> None:
    global _enabled
    _enabled = enabled


def persistence_enabled() -> bool:
    return _enabled


def _sf(engine: Engine | None = None) -> sessionmaker[Session]:
    return session_factory(engine)


def _apply_row_columns(row: EntryWatchRow, watch: EntryWatch) -> None:
    row.symbol = watch.symbol
    row.status = watch.status.value
    row.created_at = watch.created_at
    row.valid_until = watch.valid_until
    row.payload = watch.model_dump(mode="json")
    if hasattr(row, "strategy_version"):
        row.strategy_version = watch.strategy_version
    if hasattr(row, "state_version"):
        row.state_version = watch.state_version
    if hasattr(row, "trigger_version"):
        row.trigger_version = watch.trigger_version
    if hasattr(row, "claimed_at"):
        row.claimed_at = watch.claimed_at
    if hasattr(row, "claim_token"):
        row.claim_token = watch.claim_token
    if hasattr(row, "triggered_at"):
        row.triggered_at = watch.triggered_at
    if hasattr(row, "last_admission_record_id"):
        row.last_admission_record_id = watch.last_admission_record_id
    if hasattr(row, "converted_opportunity_id"):
        row.converted_opportunity_id = watch.converted_opportunity_id
    if hasattr(row, "exec_timeframe"):
        row.exec_timeframe = watch.exec_timeframe
    if hasattr(row, "geometry_hash"):
        row.geometry_hash = watch.geometry_hash


def _mark_row_expired(row: EntryWatchRow, *, reason: str) -> None:
    row.status = EntryWatchStatus.EXPIRED.value
    payload = dict(row.payload or {})
    payload["status"] = EntryWatchStatus.EXPIRED.value
    reasons = list(payload.get("reasons") or [])
    if reason not in reasons:
        reasons.append(reason)
    payload["reasons"] = reasons
    row.payload = payload


def persist_watch(watch: EntryWatch, *, engine: Engine | None = None) -> None:
    if not _enabled:
        return
    with _lock:
        SessionLocal = _sf(engine)
        with SessionLocal() as session:
            row = session.get(EntryWatchRow, watch.id)
            if row is None:
                row = EntryWatchRow(
                    id=watch.id,
                    symbol=watch.symbol,
                    status=watch.status.value,
                    created_at=watch.created_at,
                    valid_until=watch.valid_until,
                    payload=watch.model_dump(mode="json"),
                )
                session.add(row)
            _apply_row_columns(row, watch)
            try:
                session.commit()
                return
            except IntegrityError:
                session.rollback()
                if watch.status not in _ACTIONABLE:
                    raise

            cleared = (
                session.query(EntryWatchRow)
                .filter(
                    EntryWatchRow.symbol == watch.symbol,
                    EntryWatchRow.id != watch.id,
                    EntryWatchRow.status.in_([s.value for s in _ACTIONABLE]),
                )
                .all()
            )
            if not cleared:
                # Re-raise original class of failure by attempting commit path again.
                row = session.get(EntryWatchRow, watch.id)
                if row is None:
                    row = EntryWatchRow(
                        id=watch.id,
                        symbol=watch.symbol,
                        status=watch.status.value,
                        created_at=watch.created_at,
                        valid_until=watch.valid_until,
                        payload=watch.model_dump(mode="json"),
                    )
                    session.add(row)
                _apply_row_columns(row, watch)
                session.commit()
                return

            for old in cleared:
                _mark_row_expired(old, reason="SUPERSEDED_UNIQUE_ACTIVE")
            session.commit()

            row = session.get(EntryWatchRow, watch.id)
            if row is None:
                row = EntryWatchRow(
                    id=watch.id,
                    symbol=watch.symbol,
                    status=watch.status.value,
                    created_at=watch.created_at,
                    valid_until=watch.valid_until,
                    payload=watch.model_dump(mode="json"),
                )
                session.add(row)
            _apply_row_columns(row, watch)
            session.commit()


def expire_stale_active_watches(*, engine: Engine | None = None) -> int:
    """Mark past-TTL WAITING rows expired in SQLite (heals UNIQUE desync)."""
    if not _enabled:
        return 0
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    with _lock:
        SessionLocal = _sf(engine)
        with SessionLocal() as session:
            rows = (
                session.query(EntryWatchRow)
                .filter(
                    EntryWatchRow.status == EntryWatchStatus.WAITING.value,
                    EntryWatchRow.valid_until <= now,
                )
                .all()
            )
            for row in rows:
                _mark_row_expired(row, reason="WAIT_EXPIRED")
            session.commit()
            n = len(rows)
    if n:
        logger.info("entry watch persistence: expired %d past-TTL waiting rows", n)
    return n


def hydrate_entry_watches(
    store: EntryWatchStore | None = None, *, engine: Engine | None = None
) -> int:
    """Load actionable watches from DB into the in-memory store on startup."""
    if not _enabled:
        return 0
    expire_stale_active_watches(engine=engine)
    target = store or ENTRY_WATCHES
    SessionLocal = _sf(engine)
    loaded = 0
    with SessionLocal() as session:
        rows = (
            session.query(EntryWatchRow)
            .filter(EntryWatchRow.status.in_([s.value for s in _ACTIONABLE]))
            .all()
        )
        for row in rows:
            try:
                watch = EntryWatch.model_validate(row.payload)
            except Exception:  # noqa: BLE001
                logger.warning("entry watch hydrate: skip corrupt row %s", row.id)
                continue
            if watch.status not in _ACTIONABLE:
                continue
            existing = target.get(watch.id)
            if (
                existing is None
                or existing.last_observed_at is None
                or (
                    watch.last_observed_at
                    and existing.last_observed_at
                    and watch.last_observed_at > existing.last_observed_at
                )
            ):
                target.update(watch)
                loaded += 1
    if loaded:
        logger.info("entry watch persistence: hydrated %d watches from DB", loaded)
    return loaded


def patch_entry_watch_store(
    store: EntryWatchStore,
    *,
    engine: Engine | None = None,
) -> None:
    """Wrap store mutators to persist after each change."""
    if getattr(store, "_persistence_patched", False):
        return

    orig_create = store.create_from_bundle
    orig_update = store.update
    orig_mark = store.mark

    def create_from_bundle(
        candidate: TradeCandidate,
        bundle: EntryDecisionBundle,
        *,
        ttl_minutes: int | None = None,
    ) -> EntryWatch:
        watch = orig_create(candidate, bundle, ttl_minutes=ttl_minutes)
        persist_watch(watch, engine=engine)
        return watch

    def update(watch: EntryWatch) -> EntryWatch:
        out = orig_update(watch)
        persist_watch(out, engine=engine)
        return out

    def mark(
        watch_id: UUID,
        status: EntryWatchStatus,
        *,
        reason: str | None = None,
        last_admission_record_id: UUID | None = None,
        converted_opportunity_id: UUID | None = None,
    ) -> EntryWatch | None:
        out = orig_mark(
            watch_id,
            status,
            reason=reason,
            last_admission_record_id=last_admission_record_id,
            converted_opportunity_id=converted_opportunity_id,
        )
        if out is not None:
            persist_watch(out, engine=engine)
        return out

    store.create_from_bundle = create_from_bundle  # type: ignore[method-assign]
    store.update = update  # type: ignore[method-assign]
    store.mark = mark  # type: ignore[method-assign]
    store._persistence_patched = True  # type: ignore[attr-defined]
