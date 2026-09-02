"""Adaptive TargetModel — 2R is one candidate, never the sole law."""

from __future__ import annotations

from decimal import Decimal

from core.enums import TargetReachabilityClass
from core.schemas import EntryTimingFacts, TargetPlan

# Frozen F3 initial policy.
ATR_TARGET_MULT = 1.2
MIN_RR_FLOOR = 1.4  # after costs / slippage headroom vs classic 2R


def build_target_plan(
    *,
    entry: Decimal,
    stop: Decimal,
    facts: EntryTimingFacts,
    min_rr: float = 2.0,
    historical_mfe_pct: float | None = None,
    historical_sample_size: int = 0,
) -> TargetPlan:
    risk = entry - stop
    if risk <= 0:
        raise ValueError("non-positive risk")

    two_r = entry + Decimal(str(min_rr)) * risk
    atr = facts.atr
    atr_target = None
    if atr and atr > 0:
        atr_target = Decimal(str(round(float(entry) + ATR_TARGET_MULT * atr, 4)))

    structure_target = None
    if facts.nearest_resistance is not None and facts.nearest_resistance > float(entry):
        # Leave a tick of room under resistance.
        structure_target = Decimal(str(round(facts.nearest_resistance * 0.998, 4)))

    hist_target = None
    if historical_mfe_pct is not None and historical_sample_size >= 30:
        hist_target = Decimal(str(round(float(entry) * (1.0 + historical_mfe_pct / 100.0), 4)))

    candidates: list[tuple[str, Decimal]] = [("2R", two_r)]
    if atr_target is not None and atr_target > entry:
        candidates.append(("atr", atr_target))
    if structure_target is not None and structure_target > entry:
        candidates.append(("structure", structure_target))
    if hist_target is not None and hist_target > entry:
        candidates.append(("historical_mfe", hist_target))

    # Prefer the nearest realistic upside that still clears MIN_RR_FLOOR.
    floor = entry + Decimal(str(MIN_RR_FLOOR)) * risk
    viable = [(name, px) for name, px in candidates if px >= floor]
    if not viable:
        # Fall back to the lowest candidate above entry; mark reachability later.
        viable = [(name, px) for name, px in candidates if px > entry]
    if not viable:
        viable = [("2R", two_r)]

    # Choose the *closest* viable target (most conservative / reachable).
    model, price = min(viable, key=lambda item: item[1])

    resistance_before = bool(
        facts.nearest_resistance is not None
        and facts.nearest_resistance < float(price)
        and facts.nearest_resistance > float(entry)
    )

    reachability, reasons = classify_reachability(
        entry=entry,
        target=price,
        two_r=two_r,
        facts=facts,
        historical_mfe_pct=historical_mfe_pct,
        historical_sample_size=historical_sample_size,
        resistance_before=resistance_before,
        chosen_model=model,
    )

    return TargetPlan(
        price=price,
        model=model,
        reachability=reachability,
        structure_target=structure_target,
        atr_target=atr_target,
        two_r_target=two_r,
        historical_mfe_target=hist_target,
        historical_sample_size=historical_sample_size,
        resistance_before_target=resistance_before,
        reasons=reasons,
    )


def classify_reachability(
    *,
    entry: Decimal,
    target: Decimal,
    two_r: Decimal,
    facts: EntryTimingFacts,
    historical_mfe_pct: float | None,
    historical_sample_size: int,
    resistance_before: bool,
    chosen_model: str,
) -> tuple[TargetReachabilityClass, list[str]]:
    reasons: list[str] = [f"chosen_model={chosen_model}"]
    dist_pct = float((target - entry) / entry) * 100.0

    if historical_sample_size < 30 or historical_mfe_pct is None:
        reasons.append("INSUFFICIENT_HISTORICAL_MFE")
        # Without history, structural/ATR caps make REALISTIC; blind 2R past
        # resistance is AMBITIOUS/UNREALISTIC.
        if chosen_model == "2R" and resistance_before:
            reasons.append("2R_BEYOND_RESISTANCE")
            return TargetReachabilityClass.UNREALISTIC, reasons
        if (
            chosen_model == "2R"
            and facts.distance_to_resistance_pct is not None
            and dist_pct > 1.5 * max(facts.distance_to_resistance_pct, 0.01)
        ):
            return TargetReachabilityClass.AMBITIOUS, reasons
        if chosen_model in {"structure", "atr", "historical_mfe"}:
            return TargetReachabilityClass.REALISTIC, reasons
        return TargetReachabilityClass.INSUFFICIENT_DATA, reasons

    reasons.append(f"hist_mfe_pct={historical_mfe_pct:.2f}")
    if dist_pct <= historical_mfe_pct * 0.9 and not resistance_before:
        return TargetReachabilityClass.REALISTIC, reasons
    if dist_pct <= historical_mfe_pct * 1.25:
        return TargetReachabilityClass.AMBITIOUS, reasons
    return TargetReachabilityClass.UNREALISTIC, reasons
