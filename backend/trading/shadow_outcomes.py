"""Shadow outcome tracking — path after WAIT / NO_TRADE / rejected BUY.

Continues observing price after a watch expires or is invalidated so we can
measure opportunity cost and calibrate zone-touch rates. No broker orders.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Lock
from uuid import UUID, uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from core.enums import AdmissionDecision, EntryDecision, EntryWatchStatus, SetupType
from core.schemas import EntryWatch, ShadowOutcomeRecord, TradeAdmissionResult
from database.models.desk import ShadowOutcomeRow
from database.session import session_factory

SHADOW_TRADING_DAYS = 5
MIN_CALIBRATION_SAMPLES = 100


def _shadow_until(from_ts: datetime) -> datetime:
    return from_ts + timedelta(days=SHADOW_TRADING_DAYS)


def _distance_atr_bucket(distance_atr: float | None) -> str:
    if distance_atr is None:
        return "unknown"
    if distance_atr <= 0.5:
        return "0-0.5"
    if distance_atr <= 1.0:
        return "0.5-1.0"
    if distance_atr <= 2.0:
        return "1.0-2.0"
    if distance_atr <= 4.0:
        return "2.0-4.0"
    return "4.0+"


class ShadowOutcomeStore:
    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine
        self._lock = Lock()

    def _sf(self) -> sessionmaker[Session]:
        return session_factory(self._engine)

    def _write(self, session: Session, record: ShadowOutcomeRecord) -> None:
        data = record.model_dump(mode="json")
        row = session.get(ShadowOutcomeRow, record.id)
        if row is None:
            row = ShadowOutcomeRow(
                id=record.id,
                symbol=record.symbol,
                status=record.status,
                recorded_at=record.recorded_at,
                shadow_until=record.shadow_until,
                watch_id=record.watch_id,
                payload=data,
            )
            session.add(row)
        else:
            row.status = record.status
            row.shadow_until = record.shadow_until
            row.payload = data

    def begin_from_watch(
        self,
        watch: EntryWatch,
        *,
        origin: str,
        entry_decision: EntryDecision,
        admission: TradeAdmissionResult | None = None,
        admission_record_id: UUID | None = None,
        reference_price: float | None = None,
        zone_arrival_quality: int | None = None,
        zone_arrival_type: str | None = None,
        distance_atr: float | None = None,
    ) -> ShadowOutcomeRecord:
        now = datetime.now(UTC)
        ref = reference_price or float(watch.last_price or watch.current_price_at_creation)
        admission_decision = admission.decision if admission else AdmissionDecision.WAIT
        enrichment: dict[str, object] = dict(watch.desk_enrichment or {})
        if zone_arrival_quality is None:
            raw_quality = enrichment.get("zone_arrival_quality")
            if isinstance(raw_quality, (int, float, str)):
                zone_arrival_quality = int(raw_quality)
        if zone_arrival_type is None:
            raw_type = enrichment.get("zone_arrival_type")
            if raw_type is not None:
                zone_arrival_type = str(raw_type)
        if distance_atr is None:
            raw_atr = enrichment.get("distance_atr")
            if isinstance(raw_atr, (int, float, str)):
                distance_atr = float(raw_atr)

        zone_lo = float(watch.entry_zone_low)
        zone_hi = float(watch.entry_zone_high)
        in_zone = zone_lo <= ref <= zone_hi

        record = ShadowOutcomeRecord(
            id=uuid4(),
            symbol=watch.symbol.upper(),
            watch_id=watch.id,
            admission_record_id=admission_record_id,
            recorded_at=now,
            shadow_until=_shadow_until(now),
            status="active",
            origin=origin,
            entry_decision=entry_decision,
            admission_decision=admission_decision,
            setup_type=watch.setup_type or SetupType.UNKNOWN,
            reference_price=ref,
            zone_low=zone_lo,
            zone_high=zone_hi,
            planned_entry=float(watch.planned_entry),
            planned_stop=float(watch.planned_stop),
            planned_target=float(watch.planned_target),
            zone_reached=in_zone,
            zone_reached_at=now if in_zone else None,
            time_to_zone_minutes=0 if in_zone else None,
            mfe_pct=0.0,
            mae_pct=0.0,
            last_price=ref,
            distance_atr_at_origin=distance_atr,
            zone_arrival_quality=zone_arrival_quality,
            zone_arrival_type=zone_arrival_type,
        )
        with self._lock:
            SessionLocal = self._sf()
            with SessionLocal() as session:
                self._write(session, record)
                session.commit()
        return record

    def update_price(self, symbol: str, price: float, *, now: datetime | None = None) -> int:
        """Update all active shadows for symbol with new high/low excursion."""
        now = now or datetime.now(UTC)
        sym = symbol.upper()
        updated = 0
        with self._lock:
            SessionLocal = self._sf()
            with SessionLocal() as session:
                rows = (
                    session.query(ShadowOutcomeRow)
                    .filter(
                        ShadowOutcomeRow.symbol == sym,
                        ShadowOutcomeRow.status == "active",
                        ShadowOutcomeRow.shadow_until > now,
                    )
                    .all()
                )
                for row in rows:
                    rec = ShadowOutcomeRecord.model_validate(row.payload)
                    ref = rec.reference_price
                    mfe = max(rec.mfe_pct or 0.0, (price - ref) / ref * 100.0)
                    mae = min(rec.mae_pct or 0.0, (price - ref) / ref * 100.0)
                    zone_reached = rec.zone_reached
                    zone_reached_at = rec.zone_reached_at
                    time_to_zone = rec.time_to_zone_minutes
                    if not zone_reached and rec.zone_low <= price <= rec.zone_high:
                        zone_reached = True
                        zone_reached_at = now
                        elapsed = int((now - rec.recorded_at).total_seconds() // 60)
                        time_to_zone = elapsed

                    target_hit = price >= rec.planned_target
                    stop_hit = price <= rec.planned_stop

                    rec = rec.model_copy(
                        update={
                            "last_price": price,
                            "mfe_pct": round(mfe, 4),
                            "mae_pct": round(mae, 4),
                            "zone_reached": zone_reached,
                            "zone_reached_at": zone_reached_at,
                            "time_to_zone_minutes": time_to_zone,
                            "target_hit": target_hit or rec.target_hit,
                            "stop_hit": stop_hit or rec.stop_hit,
                        }
                    )
                    self._write(session, rec)
                    updated += 1
                if updated:
                    session.commit()
        return updated

    def finalize_expired(self, *, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        n = 0
        with self._lock:
            SessionLocal = self._sf()
            with SessionLocal() as session:
                rows = (
                    session.query(ShadowOutcomeRow)
                    .filter(
                        ShadowOutcomeRow.status == "active",
                        ShadowOutcomeRow.shadow_until <= now,
                    )
                    .all()
                )
                for row in rows:
                    rec = ShadowOutcomeRecord.model_validate(row.payload)
                    rec = rec.model_copy(update={"status": "complete"})
                    self._write(session, rec)
                    row.status = "complete"
                    n += 1
                if n:
                    session.commit()
        return n

    def list_completed(self, *, limit: int = 5000) -> list[ShadowOutcomeRecord]:
        SessionLocal = self._sf()
        with SessionLocal() as session:
            rows = (
                session.query(ShadowOutcomeRow)
                .filter(ShadowOutcomeRow.status == "complete")
                .order_by(ShadowOutcomeRow.recorded_at.desc())
                .limit(limit)
                .all()
            )
            return [ShadowOutcomeRecord.model_validate(r.payload) for r in rows]


SHADOW_OUTCOMES = ShadowOutcomeStore()


def maybe_begin_shadow_for_terminal_watch(
    watch: EntryWatch,
    *,
    admission: TradeAdmissionResult | None = None,
    admission_record_id: UUID | None = None,
) -> ShadowOutcomeRecord | None:
    """Start shadow tracking when a watch reaches a terminal non-converted state."""
    terminal = {
        EntryWatchStatus.EXPIRED,
        EntryWatchStatus.INVALIDATED,
    }
    if watch.status not in terminal:
        return None

    entry_decision = EntryDecision.WAIT_FOR_ENTRY
    if watch.status is EntryWatchStatus.INVALIDATED and (
        any("NO_TRADE" in r or "REVALIDATED_NO_TRADE" in r for r in watch.reasons)
        or any("RISK_REJECT" in r for r in watch.reasons)
    ):
        entry_decision = EntryDecision.NO_TRADE

    return SHADOW_OUTCOMES.begin_from_watch(
        watch,
        origin="watch_terminal",
        entry_decision=entry_decision,
        admission=admission,
        admission_record_id=admission_record_id,
    )


def distance_atr_bucket(distance_atr: float | None) -> str:
    return _distance_atr_bucket(distance_atr)
