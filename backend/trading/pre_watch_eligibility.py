"""Pre-watch eligibility — stable gates before EntryWatchCreated."""

from __future__ import annotations

from dataclasses import dataclass

from core.enums import AdmissionDecision, RiskVerdict
from core.schemas import TradeAdmissionResult
from risk.risk_engine import RiskContext
from trading.decision_precedence import (
    DATA_BLOCKED_CODES,
    TERMINAL_NO_TRADE_CODES,
    has_data_blocked_signal,
    has_terminal_no_trade,
)


@dataclass(frozen=True)
class PreWatchEligibility:
    eligible: bool
    outcome: str
    reason_codes: tuple[str, ...]


def evaluate_pre_watch_eligibility(
    admission: TradeAdmissionResult | None,
    *,
    risk_verdict: RiskVerdict | None = None,
    risk_reasons: list[str] | None = None,
    context: RiskContext | None = None,
) -> PreWatchEligibility:
    """WAIT may only be created when stable mandatory gates are provably passable."""
    reasons: list[str] = []

    if admission is None:
        return PreWatchEligibility(False, "DATA_BLOCKED", ("ADMISSION_MISSING",))

    if admission.decision is AdmissionDecision.DATA_BLOCKED:
        return PreWatchEligibility(
            False,
            "DATA_BLOCKED",
            tuple(admission.reason_codes[:8] or ("DATA_BLOCKED",)),
        )

    if admission.decision is AdmissionDecision.NO_TRADE:
        return PreWatchEligibility(
            False,
            "NO_TRADE",
            tuple(admission.reason_codes[:8] or ("NO_TRADE",)),
        )

    if admission.decision is not AdmissionDecision.WAIT:
        return PreWatchEligibility(False, "NO_TRADE", ("ADMISSION_NOT_WAIT",))

    hard = frozenset(admission.vetoes)
    if has_data_blocked_signal(hard, list(admission.reason_codes)):
        return PreWatchEligibility(
            False,
            "DATA_BLOCKED",
            tuple(admission.reason_codes[:8]),
        )

    if has_terminal_no_trade(hard, list(admission.reason_codes)):
        return PreWatchEligibility(
            False,
            "NO_TRADE",
            tuple(admission.reason_codes[:8]),
        )

    if hard & TERMINAL_NO_TRADE_CODES:
        return PreWatchEligibility(False, "NO_TRADE", tuple(sorted(hard & TERMINAL_NO_TRADE_CODES)))

    if context is not None:
        if not context.positions_trusted:
            reasons.append("POSITIONS_UNREADABLE")
        if not context.unresolved_intents_trusted:
            reasons.append("UNRESOLVED_INTENTS_UNREADABLE")

    if reasons:
        return PreWatchEligibility(False, "DATA_BLOCKED", tuple(reasons))

    if risk_verdict is not None and risk_verdict is not RiskVerdict.PASS:
        return PreWatchEligibility(
            False,
            "NO_TRADE",
            tuple(risk_reasons[:6] if risk_reasons else ("RISK_REJECT",)),
        )

    if admission.reason_codes and any(
        c in DATA_BLOCKED_CODES or c.startswith("DATA_BLOCKED") for c in admission.reason_codes
    ):
        return PreWatchEligibility(False, "DATA_BLOCKED", tuple(admission.reason_codes[:8]))

    return PreWatchEligibility(True, "WAIT", ("PRE_WATCH_ELIGIBLE",))
