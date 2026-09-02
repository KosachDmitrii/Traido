"""Explain WHY a trade was allowed or blocked — deterministic, no LLM."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from core.enums import AdmissionDecision, DataHealthStatus
from core.schemas import (
    AdmissionExplainField,
    AdmissionRecord,
    TradeAdmissionExplain,
    TradeAdmissionResult,
)


def _status_for_pass(value: bool) -> str:
    return "pass" if value else "fail"


def _data_status_label(status: DataHealthStatus) -> str:
    if status is DataHealthStatus.HEALTHY:
        return "PASS"
    if status is DataHealthStatus.DEGRADED:
        return "DEGRADED"
    return "FAIL"


def explain_from_admission(
    *,
    symbol: str,
    admission: TradeAdmissionResult | AdmissionRecord,
    entity_type: str,
    entity_id: str,
    zone_arrival_quality: int | None = None,
    zone_arrival_type: str | None = None,
    recorded_at: datetime | None = None,
) -> TradeAdmissionExplain:
    if isinstance(admission, AdmissionRecord):
        zone_arrival_quality = zone_arrival_quality or admission.zone_arrival_quality
        zone_arrival_type = zone_arrival_type or admission.zone_arrival_type
        recorded_at = recorded_at or admission.recorded_at

    decision = admission.decision
    admitted = admission.admitted
    vetoes = list(admission.vetoes)
    reasons = list(admission.reason_codes)

    if decision is AdmissionDecision.BUY_ALLOWED:
        headline = "WHY WAS THIS TRADE ALLOWED?"
    elif decision is AdmissionDecision.DATA_BLOCKED:
        headline = "WHY WAS DATA BLOCKED?"
    elif decision is AdmissionDecision.WAIT:
        headline = "WHY IS BUY STILL WAITING?"
    else:
        headline = "WHY WAS BUY PROHIBITED?"

    fields: list[AdmissionExplainField] = [
        AdmissionExplainField(
            label="Setup type", value=str(admission.setup_type.value), status="info"
        ),
        AdmissionExplainField(
            label="Setup quality",
            value=str(admission.setup_quality),
            status="pass" if admission.setup_quality >= 60 else "warn",
        ),
        AdmissionExplainField(
            label="Entry quality",
            value=str(admission.entry_quality),
            status="pass" if admission.entry_quality >= 55 else "warn",
        ),
    ]

    if zone_arrival_quality is not None:
        fields.append(
            AdmissionExplainField(
                label="Arrival",
                value=f"{zone_arrival_quality}"
                + (f" ({zone_arrival_type})" if zone_arrival_type else ""),
                status="pass" if zone_arrival_quality >= 60 else "fail",
            )
        )

    rr = admission.effective_rr
    fields.extend(
        [
            AdmissionExplainField(
                label="Effective R:R",
                value=f"{rr:.2f}" if rr is not None else "n/a",
                status="pass" if rr is not None and rr >= 2.0 else "fail",
            ),
            AdmissionExplainField(label="Chase", value=str(admission.chase_score), status="info"),
            AdmissionExplainField(
                label="Structure",
                value="PASS" if admission.structure_valid else "FAIL",
                status=_status_for_pass(admission.structure_valid),
            ),
            AdmissionExplainField(
                label="Stop",
                value="PASS" if admission.stop_valid else "FAIL",
                status=_status_for_pass(admission.stop_valid),
            ),
            AdmissionExplainField(
                label="Target",
                value="PASS" if admission.target_valid else "FAIL",
                status=_status_for_pass(admission.target_valid),
            ),
            AdmissionExplainField(
                label="Data",
                value=_data_status_label(admission.data_status),
                status=_status_for_pass(admission.data_status is DataHealthStatus.HEALTHY),
            ),
            AdmissionExplainField(
                label="Vetoes",
                value=", ".join(vetoes) if vetoes else "none",
                status="fail" if vetoes else "pass",
            ),
            AdmissionExplainField(
                label="Decision",
                value=decision.value,
                status="pass" if admitted else "fail",
            ),
        ]
    )

    return TradeAdmissionExplain(
        entity_type=entity_type,
        entity_id=entity_id,
        symbol=symbol.upper(),
        headline=headline,
        decision=decision,
        admitted=admitted,
        fields=fields,
        vetoes=vetoes,
        reason_codes=reasons,
        admission_version=admission.admission_version,
        recorded_at=recorded_at,
    )


def explain_trade_admission(
    *,
    watch_id: UUID | None = None,
    opportunity_id: UUID | None = None,
    admission_record_id: UUID | None = None,
) -> TradeAdmissionExplain | None:
    """Answer WHY for a watch, opportunity, or stored admission record."""
    from trading import admission_records as admission_mod

    records = admission_mod.ADMISSION_RECORDS

    if admission_record_id is not None:
        rec = records.get(admission_record_id)
        if rec is None:
            return None
        entity_type = "admission_record"
        entity_id = str(rec.id)
        return explain_from_admission(
            symbol=rec.symbol,
            admission=rec,
            entity_type=entity_type,
            entity_id=entity_id,
        )

    if watch_id is not None:
        rec = records.latest_for_watch(watch_id)
        if rec is None:
            from trading.entry_watches import ENTRY_WATCHES

            watch = ENTRY_WATCHES.get(watch_id)
            if watch is None:
                return None
            return TradeAdmissionExplain(
                entity_type="watch",
                entity_id=str(watch_id),
                symbol=watch.symbol,
                headline="No admission evaluation recorded yet for this watch.",
                decision=AdmissionDecision.WAIT,
                admitted=False,
                fields=[
                    AdmissionExplainField(label="Status", value=watch.status.value, status="info"),
                    AdmissionExplainField(
                        label="Setup quality at creation",
                        value=str(watch.setup_quality_at_creation),
                        status="info",
                    ),
                ],
                reason_codes=list(watch.reasons[:8]),
                recorded_at=watch.created_at,
            )
        return explain_from_admission(
            symbol=rec.symbol,
            admission=rec,
            entity_type="watch",
            entity_id=str(watch_id),
            recorded_at=rec.recorded_at,
        )

    if opportunity_id is not None:
        rec = records.latest_for_opportunity(opportunity_id)
        if rec is None:
            from trading.opportunities import OPPORTUNITIES

            opp = OPPORTUNITIES.get(opportunity_id)
            if opp is None:
                return None
            cand = opp.candidate
            return TradeAdmissionExplain(
                entity_type="opportunity",
                entity_id=str(opportunity_id),
                symbol=cand.symbol,
                headline="Legacy opportunity — admission at creation was not recorded.",
                decision=AdmissionDecision.WAIT,
                admitted=False,
                fields=[
                    AdmissionExplainField(
                        label="Entry decision",
                        value=cand.entry_decision.value
                        if cand.entry_decision is not None
                        else "n/a",
                        status="info",
                    ),
                    AdmissionExplainField(
                        label="Setup quality", value=str(cand.setup_quality or "n/a"), status="info"
                    ),
                    AdmissionExplainField(
                        label="Entry quality", value=str(cand.entry_quality or "n/a"), status="info"
                    ),
                ],
                reason_codes=list(cand.reasons[:8]),
                recorded_at=opp.created_at,
            )
        return explain_from_admission(
            symbol=rec.symbol,
            admission=rec,
            entity_type="opportunity",
            entity_id=str(opportunity_id),
            recorded_at=rec.recorded_at,
        )

    return None
