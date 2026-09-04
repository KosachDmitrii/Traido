"""Pure admission outcome precedence — capital path authority.

Priority (immutable):
  missing/stale/synthetic mandatory fact → DATA_BLOCKED
  terminal setup/geometry/policy veto     → NO_TRADE
  transient entry condition               → WAIT
  all gates PASS                          → BUY_ALLOWED (caller only)
"""

from __future__ import annotations

from core.enums import AdmissionDecision, EntryWatchStatus
from trading.outcome_taxonomy import (
    DATA_BLOCKED_CODES as TAXONOMY_DATA_BLOCKED,
)
from trading.outcome_taxonomy import (
    TERMINAL_NO_TRADE_CODES as TAXONOMY_TERMINAL,
)

# Terminal geometry/setup vetoes — never demote to WAIT.
TERMINAL_NO_TRADE_CODES = TAXONOMY_TERMINAL

# Mandatory facts unreadable or synthetic — never WAIT or NO_TRADE.
DATA_BLOCKED_CODES = TAXONOMY_DATA_BLOCKED

# Hard arrival types that are terminal, not transient wait.
TERMINAL_ARRIVAL_PREFIXES = (
    "ARRIVAL_TYPE_CRASH",
    "ARRIVAL_TYPE_GAP_DOWN",
    "ARRIVAL_TYPE_STRUCTURAL_BREAK",
)


def _code_set(codes: frozenset[str] | set[str] | list[str]) -> frozenset[str]:
    return frozenset(codes)


def has_data_blocked_signal(hard: frozenset[str], reason_codes: list[str]) -> bool:
    if hard & DATA_BLOCKED_CODES:
        return True
    return any(c in DATA_BLOCKED_CODES or c.startswith("DATA_BLOCKED") for c in reason_codes)


def has_terminal_no_trade(hard: frozenset[str], reason_codes: list[str]) -> bool:
    if hard & TERMINAL_NO_TRADE_CODES:
        return True
    return any(c in TERMINAL_NO_TRADE_CODES for c in reason_codes)


def has_transient_wait_signal(
    hard: frozenset[str],
    reason_codes: list[str],
    *,
    zone_allowed: bool,
) -> bool:
    if not zone_allowed:
        return True
    transient = {
        "ENTRY_OUTSIDE_ALLOWED_ZONE",
        "EXTREME_CHASE",
        "SPREAD_TOO_WIDE",
        "ZONE_ARRIVAL_MISSING",
        "INSUFFICIENT_EFFECTIVE_RR",
        "SETUP_QUALITY_BELOW_THRESHOLD",
        "ENTRY_QUALITY_BELOW_THRESHOLD",
        "SETUP_BELOW_FLOOR",
        "ENTRY_BELOW_FLOOR",
        "RR_BELOW_COMPENSATION_FLOOR",
        "WAITING_CONFIRMATION",
        "MOMENTUM_CONFIRMATION_MISSING",
        "VOLUME_CONFIRMATION_MISSING",
        "VWAP_CONFIRMATION_MISSING",
        "SETUP_CONFIRMATION_BELOW_FLOOR",
        "ENTRY_CONFIRMATION_BELOW_FLOOR",
        "EFFECTIVE_RR_TOO_LOW",
        "ARRIVAL_CONFIRMATION_MISSING",
        "NOT_BUY_READY",
    }
    if hard & transient:
        return True
    return any(
        c in transient
        or "ZONE_ARRIVAL" in c
        or "ARRIVAL_TYPE" in c
        or c.startswith("ZONE_ARRIVAL_QUALITY_LOW")
        for c in reason_codes
    )


def resolve_admission_decision(
    hard: frozenset[str] | set[str] | list[str],
    reason_codes: list[str],
    *,
    zone_allowed: bool = True,
) -> AdmissionDecision:
    """Map accumulated vetoes to a single admission outcome."""
    hard_f = _code_set(hard)

    if has_data_blocked_signal(hard_f, reason_codes):
        return AdmissionDecision.DATA_BLOCKED

    if has_terminal_no_trade(hard_f, reason_codes):
        return AdmissionDecision.NO_TRADE

    if any(c.startswith(p) for c in hard_f for p in TERMINAL_ARRIVAL_PREFIXES):
        return AdmissionDecision.NO_TRADE

    if hard_f or has_transient_wait_signal(hard_f, reason_codes, zone_allowed=zone_allowed):
        return AdmissionDecision.WAIT

    return AdmissionDecision.WAIT


TERMINAL_DATA_BLOCK_CODES = frozenset(
    {
        "MISSING_ATR",
        "INSUFFICIENT_BARS",
        "MISSING_VWAP",
        "MISSING_QUOTE",
        "MISSING_VOL_DIGEST",
        "POSITIONS_UNREADABLE",
        "UNRESOLVED_INTENTS_UNREADABLE",
    }
)


def watch_block_status_for_data_blocked(reason_codes: list[str]) -> EntryWatchStatus:
    if any(c in TERMINAL_DATA_BLOCK_CODES for c in reason_codes):
        return EntryWatchStatus.BLOCKED_DATA
    return EntryWatchStatus.BLOCKED_OPERATIONAL
