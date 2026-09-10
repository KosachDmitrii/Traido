"""Async final admission orchestration — full data for capital path."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
from trading.market_gate import MarketGateResult
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
    from strategy.orb.admission import final_admission

    return await final_admission(
        candidate,
        quote=quote,
        market_data=market_data,
        now=now,
        market=market,
        sector_label=sector_label,
        sector_tradable=sector_tradable,
        sector_benchmark=sector_benchmark,
        sector_provider=sector_provider,
        sector_source_ts=sector_source_ts,
        require_sector=require_sector,
        opportunity_id=opportunity_id,
        decision_version=decision_version,
    )
