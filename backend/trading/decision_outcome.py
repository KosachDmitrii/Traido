"""DecisionOutcome ledger — explain every stage of the capital funnel."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from core.enums import AdmissionDecision, EntryDecision, EntryWatchStatus, RiskVerdict


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
        return row

    def list_for_symbol(self, symbol: str, *, limit: int = 50) -> list[DecisionOutcomeRecord]:
        sym = symbol.upper()
        with self._lock:
            rows = [r for r in reversed(self._rows) if r.symbol == sym]
        return rows[:limit]

    def summary(self) -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {}
            for row in self._rows:
                key = f"{row.stage}:{row.outcome}"
                counts[key] = counts.get(key, 0) + 1
        return counts


DECISION_OUTCOMES = DecisionOutcomeLedger()
