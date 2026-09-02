"""Stop must invalidate the thesis — not be tightened for cosmetic R:R."""

from __future__ import annotations

from decimal import Decimal

from core.schemas import EntryTimingFacts, StopValidationResult

MIN_STOP_ATR = 0.25
MAX_STOP_ATR = 6.0


def validate_stop(
    *,
    entry: Decimal | float,
    stop: Decimal | float,
    facts: EntryTimingFacts,
    stop_model: str | None = None,
    structural_source: str | None = None,
    structural_level: float | None = None,
) -> StopValidationResult:
    """ATR-only distance is never structural — structure needs an explicit basis."""
    entry_f = float(entry)
    stop_f = float(stop)
    reasons: list[str] = []

    if stop_f >= entry_f:
        return StopValidationResult(
            valid=False,
            structural_basis=False,
            stop_model=stop_model or "invalid",
            structural_source=structural_source,
            structural_level=structural_level,
            reason_codes=["INVALID_STOP"],
        )

    distance_atr = facts.stop_distance_atr
    atr = facts.atr or entry_f * 0.02
    if distance_atr is None and atr > 0:
        distance_atr = (entry_f - stop_f) / atr

    atr_buffer: float | None = None
    structural = False
    model = stop_model or "unknown"
    source = structural_source
    level = structural_level

    # Explicit structural sources win.
    structural_models = {
        "structure",
        "support",
        "resistance",
        "swing",
        "swing_low",
        "swing_high",
        "breakout",
        "retest",
        "invalidation",
    }
    if model in structural_models and (
        source or level is not None or facts.nearest_support is not None
    ):
        structural = True
        if level is None and facts.nearest_support is not None:
            level = float(facts.nearest_support)
            source = source or "nearest_support"

    if facts.nearest_support is not None and stop_f <= facts.nearest_support * 1.005:
        structural = True
        source = source or "nearest_support"
        level = level if level is not None else float(facts.nearest_support)
        if model in {"unknown", "atr", "atr_multiple"}:
            model = "structure"
        if atr > 0 and level is not None:
            atr_buffer = abs(stop_f - level) / atr

    # ATR-only / distance-in-range is NOT structural.
    if model in {"atr", "atr_multiple", "n_atr"} or (
        not structural and distance_atr is not None and source is None and level is None
    ):
        structural = False
        model = model if model not in {"unknown"} else "atr"
        reasons.append("ATR_ONLY_STOP")

    if distance_atr is not None:
        if distance_atr < MIN_STOP_ATR:
            reasons.append("STOP_TOO_TIGHT")
        elif distance_atr > MAX_STOP_ATR:
            reasons.append("STOP_TOO_WIDE")

    if (
        facts.normal_expected_retrace_pct is not None
        and facts.stop_distance_pct is not None
        and facts.normal_expected_retrace_pct > facts.stop_distance_pct
    ):
        reasons.append("STOP_INSIDE_NOISE")

    valid = (
        structural
        and "STOP_TOO_TIGHT" not in reasons
        and "STOP_INSIDE_NOISE" not in reasons
        and "STOP_TOO_WIDE" not in reasons
    )
    if not valid and not structural:
        if "INVALID_STOP" not in reasons:
            reasons.append("INVALID_STOP")
        if "ATR_ONLY_STOP" not in reasons and model in {"atr", "atr_multiple", "n_atr"}:
            reasons.append("ATR_ONLY_STOP")

    return StopValidationResult(
        valid=valid,
        structural_basis=structural,
        distance_atr=distance_atr,
        stop_model=model,
        structural_source=source,
        structural_level=level,
        atr_buffer=atr_buffer,
        reason_codes=reasons,
    )
