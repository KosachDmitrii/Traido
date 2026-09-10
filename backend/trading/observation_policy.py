"""Versioned observation policy; deferred confirmations never authorize an order."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agents.trader.risk_plan import MIN_RR
from agents.trader.structure import run_structure
from agents.trader.types import TraderBundle
from core.enums import EntryDecision, Timeframe
from core.ports import MarketDataPort
from core.schemas import TradeCandidate
from quant.aggregate import aggregate_bars
from quant.engine import compute_features
from trading.effective_rr import planned_long_rr
from trading.gates import check_bar_freshness

OBSERVATION_POLICY_VERSION = "observation@2"
REQUIREMENTS = frozenset({"DESK_STRUCTURE_CONFIRMATION", "DESK_RR_CONFIRMATION"})


def prepare_observation_plan(bundle: TraderBundle) -> None:
    """Apply existing zone geometry before R:R, keeping rejected theses rejected."""
    decision = bundle._entry_decision
    if decision is None or decision.entry_decision is EntryDecision.NO_TRADE:
        return
    planned = bundle._planned
    rr = planned_long_rr(*planned) if planned else None
    needs_wait = (
        decision.entry_decision is EntryDecision.WAIT_FOR_ENTRY
        or bool(bundle.observation_requirements)
        or (rr is not None and rr < MIN_RR)
    )
    if not needs_wait:
        return
    if decision.entry_zone_low is None or decision.entry_zone_high is None:
        # Admission must reject the missing zone; never turn it into BUY.
        bundle._entry_decision = decision.model_copy(
            update={"entry_decision": EntryDecision.WAIT_FOR_ENTRY}
        )
        return
    from trading.wait_plan import derive_wait_levels

    levels = derive_wait_levels(decision)
    bundle._planned = (float(levels.entry), float(levels.stop), float(levels.target_plan.price))
    bundle._entry_decision = decision.model_copy(
        update={
            "entry_decision": EntryDecision.WAIT_FOR_ENTRY,
            "stop_price": levels.stop,
            "target": levels.target_plan,
        }
    )


async def observation_execution_reasons(
    candidate: TradeCandidate,
    market_data: MarketDataPort,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Fresh strict confirmation for observation-origin proposals, not cached UI facts."""
    requirements = set(candidate.observation_requirements)
    if not requirements:
        return []
    if requirements - REQUIREMENTS:
        return ["OBSERVATION_CONFIRMATION_DATA_MISSING"]
    reasons: list[str] = []
    if "DESK_RR_CONFIRMATION" in requirements:
        rr = (
            planned_long_rr(candidate.entry, candidate.stop, candidate.target)
            if candidate.target is not None
            else None
        )
        if rr is None or rr + 1e-6 < MIN_RR:
            reasons.append("DESK_RR_CONFIRMATION")
    if "DESK_STRUCTURE_CONFIRMATION" not in requirements:
        return reasons

    end = now or datetime.now(UTC)
    try:
        d1 = await market_data.get_bars(
            candidate.symbol, Timeframe.D1, end - timedelta(days=400), end
        )
        h1 = await market_data.get_bars(
            candidate.symbol, Timeframe.H1, end - timedelta(days=60), end
        )
        if len(d1) < 200 or len(h1) < 40:
            return [*reasons, "OBSERVATION_CONFIRMATION_DATA_MISSING"]
        if not all(
            check_bar_freshness(candidate.symbol, bars, now=end).passed for bars in (d1, h1)
        ):
            return [*reasons, "OBSERVATION_CONFIRMATION_DATA_MISSING"]
        h4 = aggregate_bars(h1, Timeframe.H4, source_label="agg:1h")
        if len(h4) < 30:
            return [*reasons, "OBSERVATION_CONFIRMATION_DATA_MISSING"]
        bundle = TraderBundle(symbol=candidate.symbol)
        bundle.features = {
            Timeframe.D1: compute_features(candidate.symbol, Timeframe.D1, d1),
            Timeframe.H4: compute_features(candidate.symbol, Timeframe.H4, h4),
        }
        if not run_structure(bundle).ok:
            reasons.append("DESK_STRUCTURE_CONFIRMATION")
    except Exception:  # noqa: BLE001 — fail closed without vendor credential text
        reasons.append("OBSERVATION_CONFIRMATION_DATA_MISSING")
    return reasons
