"""DecisionOutcome ledger — explain every stage of the capital funnel."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from core.enums import AdmissionDecision, EntryDecision, EntryWatchStatus, RiskVerdict

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DecisionOutcomeRecord:
    id: UUID
    symbol: str
    stage: str
    outcome: str
    primary_reason: str
    reason_codes: tuple[str, ...]
    admission: AdmissionDecision | None
    entry_decision: EntryDecision | None
    watch_status: EntryWatchStatus | None
    risk_verdict: RiskVerdict | None
    geometry_hash: str | None
    pipeline_run_id: UUID | None
    watch_id: UUID | None
    recorded_at: datetime


@dataclass
class DecisionOutcomeLedger:
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _rows: list[DecisionOutcomeRecord] = field(default_factory=list)

    def record(
        self,
        *,
        symbol: str,
        stage: str,
        outcome: str,
        primary_reason: str,
        reason_codes: tuple[str, ...] = (),
        admission: AdmissionDecision | None = None,
        entry_decision: EntryDecision | None = None,
        watch_status: EntryWatchStatus | None = None,
        risk_verdict: RiskVerdict | None = None,
        geometry_hash: str | None = None,
        pipeline_run_id: UUID | None = None,
        watch_id: UUID | None = None,
    ) -> DecisionOutcomeRecord:
        row = DecisionOutcomeRecord(
            id=uuid4(),
            symbol=symbol.upper(),
            stage=stage,
            outcome=outcome,
            primary_reason=primary_reason,
            reason_codes=reason_codes,
            admission=admission,
            entry_decision=entry_decision,
            watch_status=watch_status,
            risk_verdict=risk_verdict,
            geometry_hash=geometry_hash,
            pipeline_run_id=pipeline_run_id,
            watch_id=watch_id,
            recorded_at=datetime.now(UTC),
        )
        with self._lock:
            self._rows.append(row)
            if len(self._rows) > 5000:
                self._rows = self._rows[-4000:]
        self._persist(row)
        return row

    def _persist(self, row: DecisionOutcomeRecord) -> None:
        try:
            from database.models.desk import DecisionOutcomeRow
            from database.session import session_factory

            SessionLocal = session_factory()
            with SessionLocal() as session:
                session.add(
                    DecisionOutcomeRow(
                        id=row.id,
                        symbol=row.symbol,
                        stage=row.stage,
                        outcome=row.outcome,
                        primary_reason=row.primary_reason[:128],
                        recorded_at=row.recorded_at,
                        pipeline_run_id=row.pipeline_run_id,
                        watch_id=row.watch_id,
                        payload={
                            "reason_codes": list(row.reason_codes),
                            "admission": row.admission.value if row.admission else None,
                            "entry_decision": (
                                row.entry_decision.value if row.entry_decision else None
                            ),
                            "watch_status": row.watch_status.value if row.watch_status else None,
                            "risk_verdict": row.risk_verdict.value if row.risk_verdict else None,
                            "geometry_hash": row.geometry_hash,
                        },
                    )
                )
                session.commit()
        except Exception as exc:  # noqa: BLE001 — telemetry must not fail the capital path
            from core.metrics import METRICS

            METRICS.counter(
                "decision_outcome_persistence_failed",
                labels={"operation": "write"},
                help_text="DecisionOutcome database operations that failed",
            )
            logger.warning("decision outcome persistence failed (%s)", type(exc).__name__)
            return

    def list_for_symbol(self, symbol: str, *, limit: int = 50) -> list[DecisionOutcomeRecord]:
        sym = symbol.upper()
        with self._lock:
            memory = [r for r in reversed(self._rows) if r.symbol == sym]
        persisted = self._load_for_symbol(sym, limit=limit)
        if persisted is None:
            return memory[:limit]
        by_id = {row.id: row for row in persisted}
        for row in memory:
            by_id[row.id] = row
        merged = sorted(by_id.values(), key=lambda row: row.recorded_at, reverse=True)
        return merged[:limit]

    def list_recent(self, *, limit: int = 100) -> list[DecisionOutcomeRecord]:
        with self._lock:
            memory = list(reversed(self._rows))[:limit]
        persisted = self._load_recent(limit=limit)
        if persisted is None:
            return memory
        by_id = {row.id: row for row in persisted}
        for row in memory:
            by_id[row.id] = row
        return sorted(by_id.values(), key=lambda row: row.recorded_at, reverse=True)[:limit]

    def summary(self) -> dict[str, int]:
        persisted = self._load_summary()
        if persisted is not None:
            return persisted
        with self._lock:
            counts: dict[str, int] = {}
            for row in self._rows:
                key = f"{row.stage}:{row.outcome}"
                counts[key] = counts.get(key, 0) + 1
        return counts

    def _load_for_symbol(self, symbol: str, *, limit: int) -> list[DecisionOutcomeRecord] | None:
        try:
            from database.models.desk import DecisionOutcomeRow
            from database.session import session_factory

            SessionLocal = session_factory()
            with SessionLocal() as session:
                rows = (
                    session.query(DecisionOutcomeRow)
                    .filter(DecisionOutcomeRow.symbol == symbol)
                    .order_by(DecisionOutcomeRow.recorded_at.desc())
                    .limit(limit)
                    .all()
                )
            return [_record_from_row(row) for row in rows]
        except Exception as exc:  # noqa: BLE001 — RCA must not fail the capital path
            from core.metrics import METRICS

            METRICS.counter(
                "decision_outcome_persistence_failed",
                labels={"operation": "read_symbol"},
                help_text="DecisionOutcome database operations that failed",
            )
            logger.warning("decision outcome read failed (%s)", type(exc).__name__)
            return None

    def _load_summary(self) -> dict[str, int] | None:
        try:
            from sqlalchemy import func

            from database.models.desk import DecisionOutcomeRow
            from database.session import session_factory

            SessionLocal = session_factory()
            with SessionLocal() as session:
                rows = (
                    session.query(
                        DecisionOutcomeRow.stage,
                        DecisionOutcomeRow.outcome,
                        func.count(DecisionOutcomeRow.id),
                    )
                    .group_by(DecisionOutcomeRow.stage, DecisionOutcomeRow.outcome)
                    .all()
                )
            return {f"{stage}:{outcome}": int(count) for stage, outcome, count in rows}
        except Exception as exc:  # noqa: BLE001 — RCA must not fail the capital path
            from core.metrics import METRICS

            METRICS.counter(
                "decision_outcome_persistence_failed",
                labels={"operation": "summary"},
                help_text="DecisionOutcome database operations that failed",
            )
            logger.warning("decision outcome summary failed (%s)", type(exc).__name__)
            return None

    def _load_recent(self, *, limit: int) -> list[DecisionOutcomeRecord] | None:
        try:
            from database.models.desk import DecisionOutcomeRow
            from database.session import session_factory

            SessionLocal = session_factory()
            with SessionLocal() as session:
                rows = (
                    session.query(DecisionOutcomeRow)
                    .order_by(DecisionOutcomeRow.recorded_at.desc())
                    .limit(limit)
                    .all()
                )
            return [_record_from_row(row) for row in rows]
        except Exception as exc:  # noqa: BLE001 — RCA must not fail the capital path
            from core.metrics import METRICS

            METRICS.counter(
                "decision_outcome_persistence_failed",
                labels={"operation": "read_recent"},
                help_text="DecisionOutcome database operations that failed",
            )
            logger.warning("decision outcome recent read failed (%s)", type(exc).__name__)
            return None


def _optional_enum(enum_cls: type[Enum], raw: Any) -> Any:
    if raw is None or raw == "":
        return None
    try:
        return enum_cls(raw)
    except ValueError:
        return None


def _record_from_row(row: Any) -> DecisionOutcomeRecord:
    payload = row.payload if isinstance(row.payload, dict) else {}
    recorded_at = row.recorded_at
    if recorded_at is not None and recorded_at.tzinfo is None:
        recorded_at = recorded_at.replace(tzinfo=UTC)
    codes = payload.get("reason_codes") or ()
    return DecisionOutcomeRecord(
        id=row.id,
        symbol=row.symbol,
        stage=row.stage,
        outcome=row.outcome,
        primary_reason=row.primary_reason,
        reason_codes=tuple(codes),
        admission=_optional_enum(AdmissionDecision, payload.get("admission")),
        entry_decision=_optional_enum(EntryDecision, payload.get("entry_decision")),
        watch_status=_optional_enum(EntryWatchStatus, payload.get("watch_status")),
        risk_verdict=_optional_enum(RiskVerdict, payload.get("risk_verdict")),
        geometry_hash=payload.get("geometry_hash"),
        pipeline_run_id=row.pipeline_run_id,
        watch_id=row.watch_id,
        recorded_at=recorded_at,
    )


DECISION_OUTCOMES = DecisionOutcomeLedger()
