"""Lease recovery must clear stuck REVALIDATING from the in-memory desk store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from core.enums import EntryWatchStatus, SetupType
from core.schemas import EntryWatch
from trading.entry_watch_transitions import LEASE_SECONDS, lease_expired, recover_stale_leases
from trading.entry_watches import ENTRY_WATCHES, admission_claim_key


def _watch(*, status: EntryWatchStatus, lease_expired_ago: float | None = 30.0) -> EntryWatch:
    now = datetime.now(UTC)
    claimed = now - timedelta(seconds=LEASE_SECONDS + (lease_expired_ago or 0))
    lease = claimed + timedelta(seconds=LEASE_SECONDS)
    return EntryWatch(
        id=uuid4(),
        symbol="HPE",
        strategy_version="test@lease",
        created_at=now - timedelta(hours=1),
        valid_until=now + timedelta(hours=1),
        thesis="bullish",
        signal_price=Decimal("50.0"),
        current_price_at_creation=Decimal("50.0"),
        last_price=Decimal("50.2"),
        last_observed_at=now,
        entry_zone_low=Decimal("49.5"),
        entry_zone_high=Decimal("50.5"),
        planned_entry=Decimal("50.4"),
        planned_stop=Decimal("49.3"),
        planned_target=Decimal("52.7"),
        status=status,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        entry_quality_at_creation=65,
        setup_quality_at_creation=59,
        claimed_at=claimed
        if status in {EntryWatchStatus.REVALIDATING, EntryWatchStatus.CONVERTING}
        else None,
        claim_token="abc" if status is EntryWatchStatus.REVALIDATING else None,
        claim_owner_id="abc" if status is EntryWatchStatus.REVALIDATING else None,
        lease_expires_at=lease if status is EntryWatchStatus.REVALIDATING else None,
        reasons=["PRICE_ENTERS_ZONE"],
    )


def test_lease_expired_helper() -> None:
    stuck = _watch(status=EntryWatchStatus.REVALIDATING, lease_expired_ago=10)
    assert lease_expired(stuck) is True
    live = stuck.model_copy(update={"lease_expires_at": datetime.now(UTC) + timedelta(seconds=60)})
    assert lease_expired(live) is False


def test_recover_stale_leases_clears_memory_revalidating(monkeypatch) -> None:
    """Desk reads memory — DB-only recover left cards on «Повторная проверка»."""
    from trading import entry_watch_persistence as pers
    from trading import entry_watch_transitions as transitions

    monkeypatch.setattr(pers, "_enabled", False)
    monkeypatch.setattr(transitions, "persistence_enabled", lambda: False)

    ENTRY_WATCHES.clear()
    stuck = _watch(status=EntryWatchStatus.REVALIDATING, lease_expired_ago=30)
    ENTRY_WATCHES.update(stuck)

    n = recover_stale_leases()
    assert n >= 1
    got = ENTRY_WATCHES.get(stuck.id)
    assert got is not None
    assert got.status is EntryWatchStatus.TRIGGERED
    assert got.lease_expires_at is None
    assert got.claim_token is None
    assert "LEASE_EXPIRED_REVALIDATING" in got.reasons


def test_recover_stale_leases_releases_converting_admission_claim(monkeypatch) -> None:
    from trading import entry_watch_persistence as pers
    from trading import entry_watch_transitions as transitions

    monkeypatch.setattr(pers, "_enabled", False)
    monkeypatch.setattr(transitions, "persistence_enabled", lambda: False)

    ENTRY_WATCHES.clear()
    stuck = _watch(status=EntryWatchStatus.CONVERTING, lease_expired_ago=30)
    ENTRY_WATCHES.update(stuck)
    key = admission_claim_key(stuck.id, stuck.trigger_version)
    assert ENTRY_WATCHES.claim_admission(key) is True

    n = recover_stale_leases()
    assert n >= 1
    got = ENTRY_WATCHES.get(stuck.id)
    assert got is not None
    assert got.status is EntryWatchStatus.ADMITTED
    assert "LEASE_EXPIRED_CONVERTING" in got.reasons
    assert ENTRY_WATCHES.claim_admission(key) is True
