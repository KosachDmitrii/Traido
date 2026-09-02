"""Unified hard veto registry — scores cannot compensate these."""

from __future__ import annotations

from enum import StrEnum


class VetoCategory(StrEnum):
    DATA = "DATA"
    SETUP = "SETUP"
    ENTRY = "ENTRY"
    ARRIVAL = "ARRIVAL"
    STRUCTURE = "STRUCTURE"
    EXECUTION = "EXECUTION"
    RISK = "RISK"


# Category → veto codes. Hard vetoes block BUY regardless of aggressiveness.
HARD_VETO_REGISTRY: dict[str, VetoCategory] = {
    "STALE_DATA": VetoCategory.DATA,
    "MARKET_DATA_UNHEALTHY": VetoCategory.DATA,
    "INSUFFICIENT_BARS": VetoCategory.DATA,
    "STRUCTURAL_DAMAGE": VetoCategory.STRUCTURE,
    "CATALYST_INVALIDATED": VetoCategory.SETUP,
    "ENTRY_OUTSIDE_ALLOWED_ZONE": VetoCategory.ENTRY,
    "SETUP_TYPE_UNKNOWN": VetoCategory.SETUP,
    "EXTREME_CHASE": VetoCategory.ENTRY,
    "CRASH_VELOCITY": VetoCategory.ARRIVAL,
    "EXTREME_SELL_VOLUME": VetoCategory.ARRIVAL,
    "INVALID_STOP": VetoCategory.SETUP,
    "MISSING_TARGET": VetoCategory.SETUP,
    "TARGET_UNREALISTIC": VetoCategory.SETUP,
    "INSUFFICIENT_EFFECTIVE_RR": VetoCategory.EXECUTION,
    "EXTREME_SPREAD": VetoCategory.EXECUTION,
}

HARD_VETO_CODES = frozenset(HARD_VETO_REGISTRY)


def is_hard_veto(code: str) -> bool:
    return code in HARD_VETO_CODES


def vetoes_from_codes(codes: list[str]) -> list[str]:
    return [c for c in codes if is_hard_veto(c)]
