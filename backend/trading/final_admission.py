"""Async final admission orchestration — full data for capital path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from core.enums import Timeframe
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
    require_sector: bool = True,
    opportunity_id: UUID | None = None,
    decision_version: int = 0,
) -> FinalAdmissionEvaluation:
    """Fetch bars + regime, then run full final_pretrade_validation.

    Only production capital-path entry to final admission. Synthetic bars,
    invented SMAs, or scan-time market labels alone are not sufficient.
    Never stamps evaluated_at onto an assessment that arrived without one —
    that would launder FRED_NOT_CONFIGURED into a tradable regime.
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
    if assessment is not None:
        sec_label = sec_label if sec_label is not None else assessment.sector_label
        sec_tradable = sec_tradable if sec_tradable is not None else assessment.sector_tradable

    gate = evaluate_market_gate(
        assessment,
        now=evaluated_at,
        sector_label=sec_label,
        sector_tradable=sec_tradable,
        require_sector=require_sector,
    )

    exec_snap: FeatureSnapshot | None = None
    if bars_count >= MIN_BARS_FOR_FEATURES and bars:
        exec_snap = compute_features(candidate.symbol, tf, bars)

    snap = None
    if candidate.admission_snapshot:
        snap = AdmissionSnapshot.model_validate(candidate.admission_snapshot)

    gh = geometry_hash_from_candidate(candidate, exec_timeframe=tf.value)
    admission, admission_input = final_pretrade_validation(
        candidate,
        quote=quote,
        snapshot=snap,
        bars_count=bars_count,
        last_bar_ts=last_bar_ts,
        now=evaluated_at,
        exec_snap=exec_snap,
        market_gate=gate,
        market=assessment,
        sector_label=sec_label,
        sector_tradable=sec_tradable,
        bar_timeframe=tf.value,
        geometry_hash=gh,
        opportunity_id=opportunity_id,
        decision_version=decision_version,
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
    )
