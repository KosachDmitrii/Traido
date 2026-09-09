"""Zone-coherent WAIT plan — entry/stop/target aligned with the pullback zone."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from core.enums import EntryDecision, InstrumentThesis, SetupType
from core.schemas import EntryDecisionBundle, EntryWatch, TargetPlan, TradeCandidate
from trading.entry_watches import REWARD_RISK_DROPPED
from trading.historical_mfe import lookup_mfe
from trading.target_model import build_target_plan


@dataclass(frozen=True)
class WaitPlanLevels:
    entry: Decimal
    stop: Decimal
    target: Decimal
    risk_reward: float
    target_plan: TargetPlan


def needs_wait_plan(bundle: EntryDecisionBundle, candidate: TradeCandidate) -> bool:
    """Plan an intact bullish pullback at its zone, never at a chased price.

    This only selects geometry for admission. It does not pass structure,
    target, data, risk or execution gates, and never rescues a NO_TRADE thesis.
    """
    if bundle.entry_zone_low is None or bundle.entry_zone_high is None:
        return False
    if candidate.entry_decision is EntryDecision.WAIT_FOR_ENTRY:
        return True
    if (
        candidate.entry_decision is EntryDecision.NO_TRADE
        or bundle.entry_decision is EntryDecision.NO_TRADE
        or bundle.thesis is not InstrumentThesis.BULLISH
        or candidate.setup_type is not SetupType.PULLBACK_CONTINUATION
        or bundle.facts.current_price <= float(bundle.entry_zone_high)
    ):
        return False
    from trading.trade_admission import entry_allowed_for_setup_type

    allowed, reasons = entry_allowed_for_setup_type(
        candidate.setup_type,
        bundle.facts.current_price,
        float(bundle.entry_zone_low),
        float(bundle.entry_zone_high),
        bundle.facts.atr,
    )
    return not allowed and reasons == ["ENTRY_OUTSIDE_ALLOWED_ZONE"]


def derive_wait_levels(
    bundle: EntryDecisionBundle,
    candidate: TradeCandidate | None = None,
    *,
    min_rr: float = 2.0,
) -> WaitPlanLevels:
    """Build entry/stop/target from the F3 zone, not the SMA20 chase entry."""
    assert bundle.entry_zone_low is not None and bundle.entry_zone_high is not None
    facts = bundle.facts
    zone_lo = float(bundle.entry_zone_low)
    zone_hi = float(bundle.entry_zone_high)
    atr = facts.atr if facts.atr and facts.atr > 0 else max(zone_hi - zone_lo, 0.01)

    entry_f = zone_hi if zone_hi > zone_lo else (zone_lo + zone_hi) / 2.0
    from trading.entry_policy import get_entry_thresholds

    th = get_entry_thresholds()
    stop_f = zone_lo - th.zone_stop_atr * atr
    if stop_f >= entry_f:
        stop_f = zone_lo - 0.15 * atr
    if stop_f >= entry_f:
        stop_f = entry_f - max(0.25 * atr, 0.01)

    entry = Decimal(str(round(entry_f, 4)))
    stop = Decimal(str(round(stop_f, 4)))
    risk = entry - stop
    if risk <= 0:
        raise ValueError("non-positive wait plan risk")

    version = candidate.strategy_version if candidate else None
    hist_mfe, hist_n = lookup_mfe(strategy_version=version, horizon_min=60)
    target_plan = build_target_plan(
        entry=entry,
        stop=stop,
        facts=facts,
        min_rr=min_rr,
        historical_mfe_pct=hist_mfe,
        historical_sample_size=hist_n,
    )
    target = target_plan.price
    rr = float((target - entry) / risk)
    return WaitPlanLevels(
        entry=entry,
        stop=stop,
        target=target,
        risk_reward=round(rr, 2),
        target_plan=target_plan,
    )


def stale_invalidate_reason(watch: EntryWatch, price: float) -> str | None:
    """Invalidate when the zone-entry plan no longer has upside at the live price."""
    target = float(watch.planned_target)
    zone_hi = float(watch.entry_zone_high)
    if price >= target:
        return REWARD_RISK_DROPPED
    if price > zone_hi and target <= price:
        return REWARD_RISK_DROPPED
    return None
