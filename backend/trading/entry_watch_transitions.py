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

LEASE_SECONDS = 45

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


def lease_expired(watch: EntryWatch, *, now: datetime | None = None) -> bool:
    """True when a REVALIDATING/CONVERTING lease is past its deadline."""
    if watch.status not in _LEASE_STATUSES:
        return False
    now = now or datetime.now(UTC)
    lease_exp = watch.lease_expires_at
    if lease_exp is not None:
        exp = lease_exp if lease_exp.tzinfo else lease_exp.replace(tzinfo=UTC)
        return exp <= now
    claimed = watch.claimed_at
    if claimed is not None:
        c = claimed if claimed.tzinfo else claimed.replace(tzinfo=UTC)
        return c + timedelta(seconds=LEASE_SECONDS) <= now
    # Lease status with no claim timestamps — treat as stuck.
    return True


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


def _watch_from_row(row: EntryWatchRow) -> EntryWatch | None:
    """Prefer column status/version over payload — they can drift after a failed sync."""
    try:
        watch = EntryWatch.model_validate(row.payload)
    except Exception:  # noqa: BLE001
        logger.warning("lease recovery: skip corrupt watch row %s", row.id)
        return None
    patch: dict[str, object] = {
        "status": EntryWatchStatus(row.status),
        "state_version": int(row.state_version or watch.state_version),
    }
    if hasattr(row, "claim_token"):
        patch["claim_token"] = row.claim_token
    if hasattr(row, "claimed_at"):
        patch["claimed_at"] = row.claimed_at
    if hasattr(row, "claim_owner_id"):
        patch["claim_owner_id"] = getattr(row, "claim_owner_id", None)
    if hasattr(row, "lease_expires_at"):
        patch["lease_expires_at"] = getattr(row, "lease_expires_at", None)
    return watch.model_copy(update=patch)


def recover_stale_leases(*, engine: Engine | None = None) -> int:
    """Return REVALIDATING→TRIGGERED and CONVERTING→ADMITTED when lease expired.

    Always syncs the in-memory store. A DB-only recover left the desk card stuck
    on «Повторная проверка» while SQLite already said triggered.
    """
    from trading.entry_watches import ENTRY_WATCHES

    now = datetime.now(UTC)
    recovered = 0

    # 1) In-memory first — this is what the desk and watch loop read.
    for watch in ENTRY_WATCHES.list_actionable():
        if not lease_expired(watch, now=now):
            continue
        if watch.status is EntryWatchStatus.REVALIDATING:
            target = EntryWatchStatus.TRIGGERED
            reason = "LEASE_EXPIRED_REVALIDATING"
        elif watch.status is EntryWatchStatus.CONVERTING:
            target = EntryWatchStatus.ADMITTED
            reason = "LEASE_EXPIRED_CONVERTING"
        else:
            continue
        out = ENTRY_WATCHES.mark(watch.id, target, reason=reason)
        if out is None:
            # Memory/DB version skew: force memory onto the safe target.
            cleared = watch.model_copy(
                update={
                    "status": target,
                    "reasons": [*watch.reasons, reason],
                    "state_version": watch.state_version + 1,
                    "claimed_at": None,
                    "claim_token": None,
                    "claim_owner_id": None,
                    "lease_expires_at": None,
                }
            )
            ENTRY_WATCHES.update(cleared)
            out = cleared
        recovered += 1
        logger.info(
            "entry watch lease recovery (memory): %s %s → %s",
            watch.symbol,
            watch.status.value,
            target.value,
        )

    # 2) DB orphans (row leased, not present or still leased in SQL).
    if persistence_enabled():
        SessionLocal = _sf(engine)
        with SessionLocal() as session:
            rows = (
                session.query(EntryWatchRow)
                .filter(EntryWatchRow.status.in_(["revalidating", "converting"]))
                .all()
            )
            for row in rows:
                row_watch = _watch_from_row(row)
                if row_watch is None or not lease_expired(row_watch, now=now):
                    continue
                if row.status == EntryWatchStatus.REVALIDATING.value:
                    target = EntryWatchStatus.TRIGGERED
                    reason = "LEASE_EXPIRED_REVALIDATING"
                else:
                    target = EntryWatchStatus.ADMITTED
                    reason = "LEASE_EXPIRED_CONVERTING"
                out = try_transition(row_watch, target, reason=reason, engine=engine)
                if out is None:
                    continue
                ENTRY_WATCHES.update(out)
                recovered += 1
                logger.info(
                    "entry watch lease recovery (db): %s → %s",
                    row_watch.symbol,
                    target.value,
                )

    if recovered:
        logger.info("entry watch lease recovery: %d watches", recovered)
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
