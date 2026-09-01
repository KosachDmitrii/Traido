"""In-memory EntryWatch store — WAIT_FOR_ENTRY plans that must not place orders."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from core.enums import EntryWatchStatus
from core.schemas import EntryDecisionBundle, EntryWatch, TradeCandidate

# Machine-readable wait / invalidate conditions (F3 frozen set).
PRICE_ENTERS_ZONE = "PRICE_ENTERS_ZONE"
VWAP_HOLDS = "VWAP_HOLDS"
SUPPORT_HOLDS = "SUPPORT_HOLDS"
MOMENTUM_TURNS_POSITIVE = "MOMENTUM_TURNS_POSITIVE"
SPREAD_ACCEPTABLE = "SPREAD_ACCEPTABLE"
MARKET_ALIGNMENT_VALID = "MARKET_ALIGNMENT_VALID"

SUPPORT_BREAK = "SUPPORT_BREAK"
THESIS_INVALIDATED = "THESIS_INVALIDATED"
REWARD_RISK_DROPPED = "REWARD_RISK_DROPPED"
WAIT_EXPIRED = "WAIT_EXPIRED"

DEFAULT_REQUIRED = [
    PRICE_ENTERS_ZONE,
    VWAP_HOLDS,
    MOMENTUM_TURNS_POSITIVE,
    SPREAD_ACCEPTABLE,
]
DEFAULT_INVALIDATING = [
    SUPPORT_BREAK,
    THESIS_INVALIDATED,
    REWARD_RISK_DROPPED,
    WAIT_EXPIRED,
]


class EntryWatchStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[UUID, EntryWatch] = {}

    def find_open(self, symbol: str) -> EntryWatch | None:
        """At most one actionable WAIT/TRIGGERED watch per symbol."""
        symbol = symbol.upper()
        with self._lock:
            self._collapse_duplicates_locked()
            for w in self._rows.values():
                if w.symbol == symbol and w.status in {
                    EntryWatchStatus.WAITING,
                    EntryWatchStatus.TRIGGERED,
                }:
                    return w
            return None

    def create_from_bundle(
        self,
        candidate: TradeCandidate,
        bundle: EntryDecisionBundle,
        *,
        ttl_minutes: int = 120,
    ) -> EntryWatch:
        """Create or refresh the single open WAIT plan for this symbol.

        BUY opportunities already refuse a second card per symbol. WAIT used to
        append a new row every scan, so the rail showed two identical GLDs.
        Refreshing the standing plan keeps TTL and levels current without
        stacking duplicates. A TRIGGERED watch is further along — a new WAIT
        must not sit beside it.
        """
        now = datetime.now(UTC)
        assert bundle.entry_zone_low is not None and bundle.entry_zone_high is not None
        symbol = candidate.symbol.upper()

        with self._lock:
            self._collapse_duplicates_locked()
            open_same = [
                w
                for w in self._rows.values()
                if w.symbol == symbol
                and w.status in {EntryWatchStatus.WAITING, EntryWatchStatus.TRIGGERED}
            ]
            triggered = [w for w in open_same if w.status is EntryWatchStatus.TRIGGERED]
            waiting = [w for w in open_same if w.status is EntryWatchStatus.WAITING]

            if triggered:
                # Entry path already live for this symbol — do not stack a WAIT.
                return triggered[0]

            if waiting:
                primary = waiting[0]
                updated = primary.model_copy(
                    update={
                        "strategy_version": candidate.strategy_version,
                        "valid_until": now + timedelta(minutes=ttl_minutes),
                        "thesis": bundle.thesis,
                        "signal_price": candidate.signal_price or candidate.entry,
                        "current_price_at_creation": Decimal_safe(bundle.facts.current_price),
                        "entry_zone_low": bundle.entry_zone_low,
                        "entry_zone_high": bundle.entry_zone_high,
                        "planned_entry": candidate.entry,
                        "planned_stop": candidate.stop,
                        "planned_target": candidate.target,
                        "required_conditions": list(DEFAULT_REQUIRED),
                        "invalidating_conditions": list(DEFAULT_INVALIDATING),
                        "entry_quality_at_creation": bundle.entry_quality,
                        "pipeline_run_id": candidate.pipeline_run_id,
                        "candidate": candidate,
                        "reasons": list(bundle.reasons),
                    }
                )
                self._rows[primary.id] = updated
                return updated

            watch = EntryWatch(
                id=uuid4(),
                symbol=symbol,
                strategy_version=candidate.strategy_version,
                created_at=now,
                valid_until=now + timedelta(minutes=ttl_minutes),
                thesis=bundle.thesis,
                signal_price=candidate.signal_price or candidate.entry,
                current_price_at_creation=Decimal_safe(bundle.facts.current_price),
                entry_zone_low=bundle.entry_zone_low,
                entry_zone_high=bundle.entry_zone_high,
                planned_entry=candidate.entry,
                planned_stop=candidate.stop,
                planned_target=candidate.target,
                required_conditions=list(DEFAULT_REQUIRED),
                invalidating_conditions=list(DEFAULT_INVALIDATING),
                entry_quality_at_creation=bundle.entry_quality,
                status=EntryWatchStatus.WAITING,
                pipeline_run_id=candidate.pipeline_run_id,
                candidate=candidate,
                reasons=list(bundle.reasons),
            )
            self._rows[watch.id] = watch
            return watch

    def list_open(self) -> list[EntryWatch]:
        """WAITING watches still inside TTL (expires stale ones as a side effect)."""
        return [w for w in self._list_actionable_locked() if w.status is EntryWatchStatus.WAITING]

    def list_actionable(self) -> list[EntryWatch]:
        """WAITING + TRIGGERED — what the background poller must touch."""
        return self._list_actionable_locked()

    def _collapse_duplicates_locked(self) -> None:
        """Keep one actionable watch per symbol; invalidate the rest.

        Prefer TRIGGERED over WAITING (further along), then the newest
        `created_at`. Called under the store lock.
        """
        by_symbol: dict[str, list[EntryWatch]] = {}
        for w in self._rows.values():
            if w.status in {EntryWatchStatus.WAITING, EntryWatchStatus.TRIGGERED}:
                by_symbol.setdefault(w.symbol, []).append(w)
        for rows in by_symbol.values():
            if len(rows) < 2:
                continue
            rows.sort(
                key=lambda w: (
                    0 if w.status is EntryWatchStatus.TRIGGERED else 1,
                    -(w.created_at.timestamp() if w.created_at else 0),
                )
            )
            for extra in rows[1:]:
                self._rows[extra.id] = extra.model_copy(
                    update={
                        "status": EntryWatchStatus.INVALIDATED,
                        "reasons": [*extra.reasons, "SUPERSEDED_SAME_SYMBOL"],
                    }
                )

    def _list_actionable_locked(self) -> list[EntryWatch]:
        now = datetime.now(UTC)
        with self._lock:
            self._collapse_duplicates_locked()
            out: list[EntryWatch] = []
            for w in self._rows.values():
                if w.status is EntryWatchStatus.WAITING and w.valid_until <= now:
                    expired = w.model_copy(
                        update={
                            "status": EntryWatchStatus.EXPIRED,
                            "reasons": [*w.reasons, WAIT_EXPIRED],
                        }
                    )
                    self._rows[w.id] = expired
                    continue
                if w.status in {EntryWatchStatus.WAITING, EntryWatchStatus.TRIGGERED}:
                    out.append(w)
            return list(out)
    def status_counts(self) -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {}
            for w in self._rows.values():
                key = w.status.value
                counts[key] = counts.get(key, 0) + 1
            return counts


    def get(self, watch_id: UUID) -> EntryWatch | None:
        with self._lock:
            return self._rows.get(watch_id)

    def update(self, watch: EntryWatch) -> EntryWatch:
        with self._lock:
            self._rows[watch.id] = watch
            return watch

    def mark(
        self,
        watch_id: UUID,
        status: EntryWatchStatus,
        *,
        reason: str | None = None,
    ) -> EntryWatch | None:
        with self._lock:
            w = self._rows.get(watch_id)
            if w is None:
                return None
            reasons = list(w.reasons)
            if reason:
                reasons.append(reason)
            updated = w.model_copy(update={"status": status, "reasons": reasons})
            self._rows[watch_id] = updated
            return updated

    def clear(self) -> None:
        """Drop all rows — tests only."""
        with self._lock:
            self._rows.clear()


def Decimal_safe(x: float):
    from decimal import Decimal

    return Decimal(str(round(x, 4)))


def price_in_zone(price: float, watch: EntryWatch) -> bool:
    return float(watch.entry_zone_low) <= price <= float(watch.entry_zone_high)


ENTRY_WATCHES = EntryWatchStore()
