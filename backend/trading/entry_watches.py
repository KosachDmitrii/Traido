"""In-memory EntryWatch store — WAIT_FOR_ENTRY plans that must not place orders."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from core.enums import EntryWatchStatus, SetupType
from core.schemas import AdmissionSnapshot, EntryDecisionBundle, EntryWatch, TradeCandidate

# Machine-readable wait / invalidate conditions (F3 frozen set).
PRICE_ENTERS_ZONE = "PRICE_ENTERS_ZONE"
VWAP_HOLDS = "VWAP_HOLDS"
SUPPORT_HOLDS = "SUPPORT_HOLDS"
MOMENTUM_TURNS_POSITIVE = "MOMENTUM_TURNS_POSITIVE"
PULLBACK_VOL_DIGESTING = "PULLBACK_VOL_DIGESTING"
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
    PULLBACK_VOL_DIGESTING,
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
        self._admitted_keys: set[str] = set()

    def claim_admission(self, key: str) -> bool:
        """Idempotent admission — one trigger → one BUY opportunity."""
        with self._lock:
            if key in self._admitted_keys:
                return False
            self._admitted_keys.add(key)
            return True

    def find_open(self, symbol: str) -> EntryWatch | None:
        """At most one actionable WAIT/TRIGGERED watch per symbol."""
        symbol = symbol.upper()
        with self._lock:
            self._collapse_duplicates_locked()
            for w in self._rows.values():
                if w.symbol == symbol and w.status in {
                    EntryWatchStatus.WAITING,
                    EntryWatchStatus.TRIGGERED,
                    EntryWatchStatus.ADMITTED,
                    EntryWatchStatus.CONVERTING,
                }:
                    return w
            return None

    def create_from_bundle(
        self,
        candidate: TradeCandidate,
        bundle: EntryDecisionBundle,
        *,
        ttl_minutes: int | None = None,
    ) -> EntryWatch:
        """Create or refresh the single open WAIT plan for this symbol.

        BUY opportunities already refuse a second card per symbol. WAIT used to
        append a new row every scan, so the rail showed two identical GLDs.
        Refreshing the standing plan keeps TTL and levels current without
        stacking duplicates. A TRIGGERED watch is further along — a new WAIT
        must not sit beside it.
        """
        from trading.entry_policy import get_entry_thresholds

        th = get_entry_thresholds()
        if ttl_minutes is None:
            ttl_minutes = th.wait_ttl_minutes
        from trading.wait_plan import derive_wait_levels

        plan = derive_wait_levels(bundle, candidate)
        now = datetime.now(UTC)
        setup_type = candidate.setup_type if candidate else SetupType.UNKNOWN
        snapshot = AdmissionSnapshot(
            price_at_creation=float(bundle.facts.current_price),
            atr_at_creation=bundle.facts.atr,
            vwap_at_creation=(
                bundle.facts.current_price / (1 + bundle.facts.distance_from_vwap_pct / 100.0)
                if bundle.facts.distance_from_vwap_pct is not None
                else bundle.facts.anchor_price
            ),
            setup_type=setup_type,
            entry_zone_low=float(bundle.entry_zone_low) if bundle.entry_zone_low else None,
            entry_zone_high=float(bundle.entry_zone_high) if bundle.entry_zone_high else None,
            setup_quality_at_creation=bundle.setup_quality,
            entry_quality_at_creation=bundle.entry_quality,
            stop_at_creation=float(plan.stop),
            target_at_creation=float(plan.target),
            effective_rr_at_creation=plan.risk_reward,
            aggressiveness=th.aggressiveness,
            created_at=now,
            )
        aligned = None
        if candidate is not None:
            aligned = candidate.model_copy(
                update={
                    "entry": plan.entry,
                    "stop": plan.stop,
                    "target": plan.target,
                    "risk_reward": plan.risk_reward,
                    "entry_zone_low": bundle.entry_zone_low,
                    "entry_zone_high": bundle.entry_zone_high,
                }
            )
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
                        "last_price": Decimal_safe(bundle.facts.current_price),
                        "last_observed_at": now,
                        "entry_zone_low": bundle.entry_zone_low,
                        "entry_zone_high": bundle.entry_zone_high,
                        "planned_entry": plan.entry,
                        "planned_stop": plan.stop,
                        "planned_target": plan.target,
                        "planned_risk_reward": plan.risk_reward,
                        "required_conditions": list(DEFAULT_REQUIRED),
                        "invalidating_conditions": list(DEFAULT_INVALIDATING),
                        "entry_quality_at_creation": bundle.entry_quality,
                        "setup_type": setup_type,
                        "setup_quality_at_creation": bundle.setup_quality,
                        "admission_snapshot": snapshot,
                        "max_spread_bps": th.max_spread_bps,
                        "pipeline_run_id": candidate.pipeline_run_id,
                        "candidate": aligned or candidate,
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
                last_price=Decimal_safe(bundle.facts.current_price),
                last_observed_at=now,
                entry_zone_low=bundle.entry_zone_low,
                entry_zone_high=bundle.entry_zone_high,
                planned_entry=plan.entry,
                planned_stop=plan.stop,
                planned_target=plan.target,
                planned_risk_reward=plan.risk_reward,
                required_conditions=list(DEFAULT_REQUIRED),
                invalidating_conditions=list(DEFAULT_INVALIDATING),
                entry_quality_at_creation=bundle.entry_quality,
                setup_type=setup_type,
                setup_quality_at_creation=bundle.setup_quality,
                admission_snapshot=snapshot,
                max_spread_bps=th.max_spread_bps,
                status=EntryWatchStatus.WAITING,
                pipeline_run_id=candidate.pipeline_run_id,
                candidate=aligned or candidate,
                reasons=list(bundle.reasons),
            )
            from trading.entry_watch_transitions import enrich_new_watch_fields

            watch = enrich_new_watch_fields(watch)
            self._rows[watch.id] = watch
            return watch

    def list_open(self) -> list[EntryWatch]:
        """WAITING watches still inside TTL (expires stale ones as a side effect)."""
        return [w for w in self._list_actionable_locked() if w.status is EntryWatchStatus.WAITING]

    def list_for_desk(self) -> list[EntryWatch]:
        """Actionable watches — operator sees machine state through conversion."""
        with self._lock:
            self._collapse_duplicates_locked()
            now = datetime.now(UTC)
            out: list[EntryWatch] = []
            visible = {
                EntryWatchStatus.WAITING,
                EntryWatchStatus.TRIGGERED,
                EntryWatchStatus.REVALIDATING,
                EntryWatchStatus.ADMITTED,
                EntryWatchStatus.CONVERTING,
            }
            for w in self._rows.values():
                if w.status is EntryWatchStatus.WAITING and w.valid_until <= now:
                    continue
                if w.status in visible:
                    out.append(w)
            return out

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
                if w.status in {
                    EntryWatchStatus.WAITING,
                    EntryWatchStatus.TRIGGERED,
                    EntryWatchStatus.ADMITTED,
                    EntryWatchStatus.CONVERTING,
                    EntryWatchStatus.REVALIDATING,
                }:
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
        last_admission_record_id: UUID | None = None,
        converted_opportunity_id: UUID | None = None,
    ) -> EntryWatch | None:
        with self._lock:
            w = self._rows.get(watch_id)
            if w is None:
                return None
            from trading.entry_watch_transitions import try_transition

            updated = try_transition(
                w,
                status,
                reason=reason,
                last_admission_record_id=last_admission_record_id,
                converted_opportunity_id=converted_opportunity_id,
            )
            if updated is None:
                return None
            self._rows[watch_id] = updated
            return updated

    def clear(self) -> None:
        """Drop all rows — tests only."""
        with self._lock:
            self._rows.clear()
            self._admitted_keys.clear()


def Decimal_safe(x: float) -> Decimal:
    return Decimal(str(round(x, 4)))


def price_in_zone(price: float, watch: EntryWatch) -> bool:
    return float(watch.entry_zone_low) <= price <= float(watch.entry_zone_high)


ENTRY_WATCHES = EntryWatchStore()
