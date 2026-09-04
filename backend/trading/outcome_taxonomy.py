"""Canonical admission / pre-watch / auto-trigger outcome classes.

WAIT            — real candidate, entry confirmation not yet present
NO_TRADE        — facts are present; edge or setup conditions are absent
DATA_BLOCKED    — mandatory facts missing or stale; safety cannot be proven
OPERATIONAL_BLOCKED — broker, persistence, or a service is down
TERMINAL_REJECT — setup or policy refuses this name
UNKNOWN         — broker mutation may have been sent; reconcile only
"""

from __future__ import annotations

from enum import StrEnum


class OutcomeClass(StrEnum):
    WAIT = "WAIT"
    NO_TRADE = "NO_TRADE"
    DATA_BLOCKED = "DATA_BLOCKED"
    OPERATIONAL_BLOCKED = "OPERATIONAL_BLOCKED"
    TERMINAL_REJECT = "TERMINAL_REJECT"
    UNKNOWN = "UNKNOWN"


DATA_BLOCKED_CODES = frozenset(
    {
        "STALE_DATA",
        "MARKET_DATA_UNHEALTHY",
        "QUOTE_INCOMPLETE",
        "MISSING_ATR",
        "INSUFFICIENT_BARS",
        "DATA_BLOCKED",
        "MISSING_VWAP",
        "POSITIONS_UNREADABLE",
        "UNRESOLVED_INTENTS_UNREADABLE",
        "REGIME_MISSING",
        "REGIME_STALE",
        "REGIME_UNKNOWN",
        "REGIME_TIMESTAMP_MISSING",
        "REGIME_TIMESTAMP_INVALID",
        "FRED_NOT_CONFIGURED",
        "FRED_SERIES_EMPTY",
        "FRED_OBSERVATION_STALE",
        "FRED_OBSERVATION_DATE_MISSING",
        "FRED_OBSERVATION_DATE_INVALID",
        "NEWS_NOT_CONFIGURED",
        "NEWS_UNAVAILABLE",
        "NEWS_UNVERIFIED",
        "EARNINGS_CALENDAR_NOT_CONFIGURED",
        "EARNINGS_CALENDAR_UNAVAILABLE",
        "EARNINGS_UNVERIFIED",
        "SECTOR_NOT_CONFIGURED",
        "SECTOR_UNAVAILABLE",
        "SECTOR_UNVERIFIED",
        "SECTOR_UNCLASSIFIED",
        "SECTOR_ASSESSMENT_MISSING",
        "SECTOR_GATE_MISSING",
        "PORTFOLIO_STATE_UNAVAILABLE",
        "ADMISSION_MISSING",
        "MARKET_DATA_NOT_CONFIGURED",
        "LIVE_QUOTE_REQUIRED",
        "QUOTE_REQUIRED",
        "QUOTE_STALE",
        "STALE_BARS",
        "NO_BARS",
        "BARS_REQUIRED",
        "WATCH_MARK_UNAVAILABLE",
        "INSUFFICIENT_H1_BARS",
        "TOP_OF_BOOK_UNAVAILABLE",
    }
)

OPERATIONAL_BLOCKED_CODES = frozenset(
    {
        "RECONCILIATION_STALE",
        "RECONCILIATION_NEVER_RAN",
        "UNRESOLVED_BROKER_STATE",
        "KILL_SWITCH",
        "BROKER_NOT_READY",
        "BROKER_CREDENTIALS_MISSING",
        "ENTRY_IN_FLIGHT",
        "STALE_DECISION",
        "APPROVAL_IDENTITY_REQUIRED",
        "RTH_GATE_REJECTED",
        "IDEMPOTENCY_CONFLICT",
    }
)

TERMINAL_NO_TRADE_CODES = frozenset(
    {
        "STRUCTURAL_DAMAGE",
        "INVALID_GEOMETRY",
        "V1_LONG_ONLY",
        "NON_POSITIVE_EQUITY",
        "MISSING_TARGET",
        "MISSING_STOP",
        "MISSING_ENTRY_ZONE",
        "SETUP_TYPE_UNKNOWN",
        "INVALID_STOP",
        "TARGET_UNREALISTIC",
        "TARGET_PLAN_MISMATCH",
        "TARGET_NO_BASIS",
        "ATR_ONLY_STOP",
        "EXTREME_SPREAD",
        "CRASH_VELOCITY",
        "THESIS_NOT_BULLISH",
        "MATERIAL_NEGATIVE_MOMENTUM",
        "HEAVY_SELL_VOLUME",
        "CANDIDATE_SETUP_BELOW_FLOOR",
        "CANDIDATE_ENTRY_BELOW_FLOOR",
        "PLANNED_RR_BELOW_BASE_FLOOR",
        "EARNINGS_IMMINENT",
        "EARNINGS_JUST_REPORTED",
        "REGIME_NOT_TRADABLE",
        "SECTOR_BLOCKED",
        "SIZE_ZERO",
        "SIZE_BELOW_ONE_SHARE",
        "MAX_POSITION_PCT",
        "MAX_BOOK_EXPOSURE",
        "MAX_SECTOR_EXPOSURE",
        "MAX_RISK_PER_TRADE",
        "MAX_DAILY_LOSS",
        "MAX_WEEKLY_LOSS",
        "MAX_PORTFOLIO_DRAWDOWN",
        "ENTRY_ORDER_REJECTED",
        "OPERATOR_QTY_INVALID",
        "OPERATOR_QTY_ABOVE_RISK",
        "NON_POSITIVE_RISK_PER_SHARE",
        "POSITION_ALREADY_OPEN",
        "EXTERNAL_POSITION_BLOCK",
    }
)

# Errors that mean a broker mutation may already exist — never retry decide().
UNKNOWN_AFTER_SUBMIT_MARKERS = (
    "ENTRY_STATE_UNKNOWN",
    "EXIT_STATE_UNKNOWN",
    "EXIT_FILL_FAILED",
    "protective_stop_submit_race_unresolved",
    "order_intent_vanished",
    "STOP_FAILED_FLATTENED",
)

# Broker truth is known and no fill remains, but the attempt could not finish.
# Retrying is safe only after backoff; this is not UNKNOWN broker state.
OPERATIONAL_RETRY_MARKERS = ("ENTRY_FILL_FAILED",)

# Risk conditions that can clear while the same short-lived card is valid.
TRANSIENT_RISK_CODES = frozenset(
    {
        "MAX_OPEN_POSITIONS",
    }
)

# Pre-submit book/price conditions that can reverse — keep the card, retry.
# Must be checked before the generic BUY_REJECTED → TERMINAL_REJECT catch-all.
TRANSIENT_BUY_REJECT_PREFIXES = (
    "BUY_REJECTED_SPREAD",
    "BUY_REJECTED_CHASE",
    "BUY_REJECTED_RR_DROPPED",
)


def classify_codes(codes: list[str] | tuple[str, ...] | set[str] | frozenset[str]) -> OutcomeClass:
    """First matching class wins: UNKNOWN is caller-owned; then data, ops, terminal."""
    raw = [c for c in codes if c]
    if any(c in DATA_BLOCKED_CODES or c.startswith("DATA_BLOCKED") for c in raw):
        return OutcomeClass.DATA_BLOCKED
    if any(c in OPERATIONAL_BLOCKED_CODES for c in raw):
        return OutcomeClass.OPERATIONAL_BLOCKED
    if any(c in TERMINAL_NO_TRADE_CODES for c in raw):
        return OutcomeClass.NO_TRADE
    return OutcomeClass.WAIT


def classify_exception_text(text: str) -> OutcomeClass:
    blob = (text or "").upper()
    if any(marker.upper() in blob for marker in UNKNOWN_AFTER_SUBMIT_MARKERS):
        return OutcomeClass.UNKNOWN
    if any(marker in blob for marker in OPERATIONAL_RETRY_MARKERS):
        return OutcomeClass.OPERATIONAL_BLOCKED
    if any(blob.startswith(prefix) for prefix in TRANSIENT_BUY_REJECT_PREFIXES):
        return OutcomeClass.WAIT
    tokens = [part.strip() for part in blob.replace(",", ":").split(":") if part.strip()]
    tokens.append(blob)
    classified = classify_codes(tokens)
    if classified is not OutcomeClass.WAIT:
        return classified
    if blob.startswith("RISK_REJECT") and any(code in tokens for code in TRANSIENT_RISK_CODES):
        return OutcomeClass.WAIT
    if "LIQUIDITY_GATE_REJECTED" in blob or "RTH_GATE_REJECTED" in blob:
        if "MARKET_DATA_NOT_CONFIGURED" in blob or "LIVE_QUOTE" in blob or "QUOTE" in blob:
            return OutcomeClass.DATA_BLOCKED
        return OutcomeClass.OPERATIONAL_BLOCKED
    if blob.startswith("BUY_REJECTED_STALE") or "STALE_DATA" in blob:
        return OutcomeClass.DATA_BLOCKED
    if blob.startswith("BUY_REJECTED_REGIME") and "MISSING" in blob:
        return OutcomeClass.DATA_BLOCKED
    if blob.startswith("BUY_REJECTED"):
        return OutcomeClass.TERMINAL_REJECT
    if "NO_TRADE" in blob:
        return OutcomeClass.NO_TRADE
    if "DATA_BLOCKED" in blob:
        return OutcomeClass.DATA_BLOCKED
    if "WAIT" in blob:
        return OutcomeClass.WAIT
    return OutcomeClass.OPERATIONAL_BLOCKED
