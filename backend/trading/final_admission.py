"""Async final admission orchestration — full data for capital path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from core.enums import DataHealthStatus, Timeframe
from core.ports import MarketDataPort
from core.schemas import (
    AdmissionInput,
    AdmissionSnapshot,
    FeatureSnapshot,
    MarketAssessment,
    Quote,
    TradeAdmissionResult,
    TradeCandidate,
)
from quant.engine import compute_features
from trading.data_integrity import MIN_BARS_FOR_FEATURES, last_bar_timestamp
from trading.final_pretrade import final_pretrade_validation
from trading.geometry_hash import geometry_hash_from_candidate
from trading.market_gate import MarketGateResult, evaluate_market_gate
from trading.zone_arrival import ZoneArrivalFacts

ADMISSION_ORCHESTRATION_VERSION = "final_admission@1"


@dataclass
class FinalAdmissionEvaluation:
    admission: TradeAdmissionResult
    admission_input: AdmissionInput
    quote: Quote
    snapshot: AdmissionSnapshot | None
    market_gate: MarketGateResult
    bars_count: int
    evaluated_at: datetime
    last_bar_ts: datetime | None = None
    geometry_hash: str | None = None
    exec_snap: FeatureSnapshot | None = None
    zone_arrival: ZoneArrivalFacts | None = None


def _resolve_timeframe(candidate: TradeCandidate) -> Timeframe:
    tf = candidate.exec_timeframe
    if tf is None:
        return Timeframe.H1
    if isinstance(tf, Timeframe):
        return tf
    try:
        return Timeframe(str(tf))
    except ValueError:
        return Timeframe.H1


async def build_and_evaluate_final_admission(
    candidate: TradeCandidate,
    *,
    quote: Quote,
    market_data: MarketDataPort,
    now: datetime | None = None,
    market: MarketAssessment | None = None,
    sector_label: str | None = None,
    sector_tradable: bool | None = None,
    sector_benchmark: str | None = None,
    sector_provider: str | None = None,
    sector_source_ts: datetime | None = None,
    require_sector: bool = True,
    opportunity_id: UUID | None = None,
    decision_version: int = 0,
    tape_last: float | None = None,
) -> FinalAdmissionEvaluation:
    """Fetch bars + regime, then run full final_pretrade_validation.

    Macro (FRED) and sector (benchmark bars) are independent hard gates.
    Macro gate runs without sector; sector is enforced separately.
    """
    evaluated_at = now or datetime.now(UTC)
    if evaluated_at.tzinfo is None:
        evaluated_at = evaluated_at.replace(tzinfo=UTC)
    else:
        evaluated_at = evaluated_at.astimezone(UTC)

    tf = _resolve_timeframe(candidate)
    end = evaluated_at
    bars = await market_data.get_bars(candidate.symbol, tf, end - timedelta(days=60), end)
    bars_count = len(bars) if bars else 0
    last_bar_ts = last_bar_timestamp(bars)

    assessment = market
    sec_label = sector_label
    sec_tradable = sector_tradable
    # Never pull synthetic sector facts from FRED MarketAssessment.

    # Macro gate: regime only. Sector enforced below / in final_pretrade.
    gate = evaluate_market_gate(
        assessment,
        now=evaluated_at,
        sector_label=None,
        sector_tradable=None,
        require_sector=False,
    )
    # Overlay sector into the combined gate result for callers that read it.
    if require_sector:
        if sec_tradable is None:
            gate = MarketGateResult(
                tradable_long=False,
                status=DataHealthStatus.UNHEALTHY,
                market_label=gate.market_label,
                sector_label=sec_label,
                sector_tradable=None,
                evaluated_at=evaluated_at,
                regime_ts=gate.regime_ts,
                reason_codes=[*gate.reason_codes, "SECTOR_ASSESSMENT_MISSING"],
                benchmark=sector_benchmark or gate.benchmark,
            )
        elif sec_tradable is False:
            gate = MarketGateResult(
                tradable_long=False,
                status=DataHealthStatus.HEALTHY
                if gate.status is DataHealthStatus.HEALTHY
                else gate.status,
                market_label=gate.market_label,
                sector_label=sec_label,
                sector_tradable=False,
                evaluated_at=evaluated_at,
                regime_ts=gate.regime_ts,
                reason_codes=[*gate.reason_codes, "SECTOR_BLOCKED"],
                benchmark=sector_benchmark or gate.benchmark,
            )
        else:
            gate = MarketGateResult(
                tradable_long=gate.tradable_long,
                status=gate.status,
                market_label=gate.market_label,
                sector_label=sec_label,
                sector_tradable=True,
                evaluated_at=evaluated_at,
                regime_ts=gate.regime_ts,
                reason_codes=list(gate.reason_codes),
                benchmark=sector_benchmark or gate.benchmark,
            )

    exec_snap: FeatureSnapshot | None = None
    if bars_count >= MIN_BARS_FOR_FEATURES and bars:
        exec_snap = compute_features(candidate.symbol, tf, bars)

    snap = None
    if candidate.admission_snapshot:
        snap = AdmissionSnapshot.model_validate(candidate.admission_snapshot)

    gh = geometry_hash_from_candidate(candidate, exec_timeframe=tf.value)
    admission, admission_input, zone_arrival = final_pretrade_validation(
        candidate,
        quote=quote,
        snapshot=snap,
        bars_count=bars_count,
        bars=bars,
        last_bar_ts=last_bar_ts,
        now=evaluated_at,
        exec_snap=exec_snap,
        market_gate=gate,
        market=assessment,
        sector_label=sec_label,
        sector_tradable=sec_tradable,
        sector_benchmark=sector_benchmark,
        sector_provider=sector_provider,
        sector_source_ts=sector_source_ts,
        bar_timeframe=tf.value,
        geometry_hash=gh,
        opportunity_id=opportunity_id,
        decision_version=decision_version,
        tape_last=tape_last,
    )

    return FinalAdmissionEvaluation(
        admission=admission,
        admission_input=admission_input,
        quote=quote,
        snapshot=admission.snapshot or snap,
        market_gate=gate,
        bars_count=bars_count,
        evaluated_at=evaluated_at,
        last_bar_ts=last_bar_ts,
        geometry_hash=gh,
        exec_snap=exec_snap,
        zone_arrival=zone_arrival,
    )
