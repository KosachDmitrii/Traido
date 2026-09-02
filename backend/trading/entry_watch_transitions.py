"""CAS state transitions for EntryWatch — DB is source of truth when persistence is on."""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.enums import EntryWatchStatus
from core.schemas import EntryWatch
from database.models.desk import EntryWatchRow
from database.session import session_factory
from trading.entry_watch_persistence import persistence_enabled
from trading.geometry_hash import geometry_hash_from_watch

logger = logging.getLogger(__name__)

LEASE_SECONDS = 120

_ALLOWED: dict[EntryWatchStatus, set[EntryWatchStatus]] = {
    EntryWatchStatus.WAITING: {
        EntryWatchStatus.TRIGGERED,
        EntryWatchStatus.EXPIRED,
        EntryWatchStatus.INVALIDATED,
        EntryWatchStatus.CANCELLED,
    },
    EntryWatchStatus.TRIGGERED: {
        EntryWatchStatus.REVALIDATING,
        EntryWatchStatus.WAITING,
        EntryWatchStatus.INVALIDATED,
        EntryWatchStatus.EXPIRED,
        EntryWatchStatus.CANCELLED,
    },
    EntryWatchStatus.REVALIDATING: {
        EntryWatchStatus.WAITING,
        EntryWatchStatus.TRIGGERED,
        EntryWatchStatus.ADMITTED,
        EntryWatchStatus.INVALIDATED,
        EntryWatchStatus.EXPIRED,
    },
    EntryWatchStatus.ADMITTED: {
        EntryWatchStatus.CONVERTING,
        EntryWatchStatus.INVALIDATED,
        EntryWatchStatus.TRIGGERED,
    },
    EntryWatchStatus.CONVERTING: {
        EntryWatchStatus.CONVERTED,
        EntryWatchStatus.ADMITTED,
        EntryWatchStatus.INVALIDATED,
    },
}

_LEASE_STATUSES = {EntryWatchStatus.REVALIDATING, EntryWatchStatus.CONVERTING}


def _sf(engine: Engine | None = None) -> sessionmaker[Session]:
    return session_factory(engine)


def _needs_lease(to_status: EntryWatchStatus) -> bool:
    return to_status in _LEASE_STATUSES


def try_transition(
    watch: EntryWatch,
    to_status: EntryWatchStatus,
    *,
    reason: str | None = None,
    claim_token: str | None = None,
    claim_owner_id: str | None = None,
    last_admission_record_id: UUID | None = None,
    converted_opportunity_id: UUID | None = None,
    engine: Engine | None = None,
) -> EntryWatch | None:
    """Atomic SQL CAS transition. Success only when rowcount == 1."""
    from_status = watch.status
    if to_status not in _ALLOWED.get(from_status, set()):
        logger.warning(
            "watch transition denied %s → %s for %s",
            from_status.value,
            to_status.value,
            watch.id,
        )
        return None

    now = datetime.now(UTC)
    reasons = list(watch.reasons)
    if reason:
        reasons.append(reason)

    token = claim_token
    owner = claim_owner_id
    lease_expires: datetime | None = None
    if _needs_lease(to_status):
        if token is None:
            token = secrets.token_hex(16)
        if owner is None:
            owner = token
        lease_expires = now + timedelta(seconds=LEASE_SECONDS)

    extra: dict[str, object] = {
        "status": to_status,
        "reasons": reasons,
        "state_version": watch.state_version + 1,
    }
    if to_status is EntryWatchStatus.TRIGGERED and from_status is EntryWatchStatus.WAITING:
        extra["trigger_version"] = watch.trigger_version + 1
        extra["triggered_at"] = now
    if _needs_lease(to_status):
        extra["claimed_at"] = now
        extra["claim_token"] = token
        extra["claim_owner_id"] = owner
        extra["lease_expires_at"] = lease_expires
    elif from_status in _LEASE_STATUSES and to_status not in _LEASE_STATUSES:
        extra["claimed_at"] = None
        extra["claim_token"] = None
        extra["claim_owner_id"] = None
        extra["lease_expires_at"] = None
    if last_admission_record_id is not None:
        extra["last_admission_record_id"] = last_admission_record_id
    if converted_opportunity_id is not None:
        extra["converted_opportunity_id"] = converted_opportunity_id

    updated = watch.model_copy(update=extra)

    if not persistence_enabled():
        return updated

    expected_version = watch.state_version
    values: dict[str, object] = {
        "status": to_status.value,
        "state_version": expected_version + 1,
        "payload": updated.model_dump(mode="json"),
        "strategy_version": updated.strategy_version,
        "trigger_version": updated.trigger_version,
        "claimed_at": updated.claimed_at,
        "claim_token": updated.claim_token,
        "triggered_at": updated.triggered_at,
        "last_admission_record_id": updated.last_admission_record_id,
        "converted_opportunity_id": updated.converted_opportunity_id,
        "exec_timeframe": updated.exec_timeframe,
        "geometry_hash": updated.geometry_hash,
    }
    # New columns from 0009 — only set when present on the mapped class.
    if hasattr(EntryWatchRow, "claim_owner_id"):
        values["claim_owner_id"] = updated.claim_owner_id
    if hasattr(EntryWatchRow, "lease_expires_at"):
        values["lease_expires_at"] = updated.lease_expires_at

    SessionLocal = _sf(engine)
    with SessionLocal() as session:
        stmt = (
            update(EntryWatchRow)
            .where(
                EntryWatchRow.id == watch.id,
                EntryWatchRow.status == from_status.value,
                EntryWatchRow.state_version == expected_version,
            )
            .values(**values)
        )
        result = session.execute(stmt)
        if getattr(result, "rowcount", 0) != 1:
            session.rollback()
            return None
        session.commit()
    return updated


def recover_stale_leases(*, engine: Engine | None = None) -> int:
    """Return REVALIDATING→TRIGGERED and CONVERTING→ADMITTED when lease expired."""
    if not persistence_enabled():
        return 0
    now = datetime.now(UTC)
    recovered = 0
    SessionLocal = _sf(engine)
    with SessionLocal() as session:
        rows = (
            session.query(EntryWatchRow)
            .filter(EntryWatchRow.status.in_(["revalidating", "converting"]))
            .all()
        )
        for row in rows:
            lease_exp = getattr(row, "lease_expires_at", None)
            claimed = row.claimed_at
            expired = False
            if lease_exp is not None:
                exp = lease_exp if lease_exp.tzinfo else lease_exp.replace(tzinfo=UTC)
                expired = exp <= now
            elif claimed is not None:
                c = claimed if claimed.tzinfo else claimed.replace(tzinfo=UTC)
                expired = c + timedelta(seconds=LEASE_SECONDS) <= now
            if not expired:
                continue
            try:
                watch = EntryWatch.model_validate(row.payload)
            except Exception:  # noqa: BLE001
                logger.warning("lease recovery: skip corrupt watch row %s", row.id)
                continue
            if row.status == EntryWatchStatus.REVALIDATING.value:
                target = EntryWatchStatus.TRIGGERED
                reason = "LEASE_EXPIRED_REVALIDATING"
            else:
                target = EntryWatchStatus.ADMITTED
                reason = "LEASE_EXPIRED_CONVERTING"
            out = try_transition(watch, target, reason=reason, engine=engine)
            if out is not None:
                recovered += 1
    if recovered:
        logger.info("entry watch lease recovery: %d rows", recovered)
    return recovered


def enrich_new_watch_fields(watch: EntryWatch, *, exec_timeframe: str = "H1") -> EntryWatch:
    """Set geometry_hash and exec_timeframe on new/refreshed watches."""
    gh = geometry_hash_from_watch(watch)
    return watch.model_copy(
        update={
            "exec_timeframe": exec_timeframe,
            "geometry_hash": gh,
        }
    )
