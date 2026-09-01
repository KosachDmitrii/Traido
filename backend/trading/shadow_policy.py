"""Paper OLD vs NEW entry-policy shadow comparison — no duplicate broker orders."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from core.audit import create_audit
from core.enums import EntryDecision, InstrumentThesis, SessionCohort
from core.schemas import ShadowPolicyRecord, TradeCandidate

_LOCK = threading.Lock()
_PATH = Path(__file__).resolve().parents[1] / "data" / "shadow_entry_policy.jsonl"


def _append_jsonl(rec: ShadowPolicyRecord) -> None:
    line = rec.model_dump(mode="json")
    with _LOCK:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        with _PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")


async def record_shadow_async(
    *,
    candidate: TradeCandidate,
    old_policy: EntryDecision,
    new_policy: EntryDecision,
    thesis: InstrumentThesis,
    session_cohort: SessionCohort,
    entry_quality: int | None,
    chase_reasons: list[str],
    reasons: list[str],
) -> ShadowPolicyRecord:
    rec = ShadowPolicyRecord(
        symbol=candidate.symbol.upper(),
        recorded_at=datetime.now(UTC),
        session_cohort=session_cohort,
        strategy_version=candidate.strategy_version,
        pipeline_run_id=candidate.pipeline_run_id,
        old_policy=old_policy,
        new_policy=new_policy,
        thesis=thesis,
        signal_price=candidate.signal_price or candidate.entry,
        proposed_entry=candidate.entry,
        proposed_stop=candidate.stop,
        proposed_target=candidate.target,
        entry_quality=entry_quality,
        chase_reasons=list(chase_reasons),
        reasons=list(reasons),
    )
    _append_jsonl(rec)
    audit = create_audit()
    await audit.append(
        "ShadowPolicyRecorded",
        "entry_timing",
        rec.model_dump(mode="json"),
        pipeline_run_id=candidate.pipeline_run_id,
        entity_type="symbol",
        entity_id=rec.symbol,
    )
    return rec
