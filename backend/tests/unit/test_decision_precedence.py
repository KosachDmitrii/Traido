"""P0 decision precedence — terminal vetoes never demote to WAIT."""

from __future__ import annotations

from core.enums import AdmissionDecision, EntryWatchStatus
from trading.decision_precedence import (
    resolve_admission_decision,
    watch_block_status_for_data_blocked,
)


def test_target_unrealistic_is_no_trade_even_with_zone_arrival_missing() -> None:
    hard = frozenset({"TARGET_UNREALISTIC", "ZONE_ARRIVAL_MISSING"})
    decision = resolve_admission_decision(
        hard,
        ["TARGET_UNREALISTIC", "ZONE_ARRIVAL_MISSING"],
        zone_allowed=False,
    )
    assert decision is AdmissionDecision.NO_TRADE


def test_structural_damage_is_no_trade() -> None:
    decision = resolve_admission_decision(
        frozenset({"STRUCTURAL_DAMAGE"}),
        ["STRUCTURAL_DAMAGE"],
    )
    assert decision is AdmissionDecision.NO_TRADE


def test_insufficient_bars_is_data_blocked() -> None:
    decision = resolve_admission_decision(
        frozenset({"INSUFFICIENT_BARS"}),
        ["INSUFFICIENT_BARS", "DATA_BLOCKED"],
    )
    assert decision is AdmissionDecision.DATA_BLOCKED


def test_transient_spread_is_wait_not_no_trade() -> None:
    decision = resolve_admission_decision(
        frozenset({"SPREAD_TOO_WIDE"}),
        ["SPREAD_TOO_WIDE"],
        zone_allowed=True,
    )
    assert decision is AdmissionDecision.WAIT


def test_data_blocked_beats_wait() -> None:
    decision = resolve_admission_decision(
        frozenset({"STALE_DATA", "SPREAD_TOO_WIDE"}),
        ["STALE_DATA", "DATA_BLOCKED"],
    )
    assert decision is AdmissionDecision.DATA_BLOCKED


def test_stale_quote_is_a_data_block_not_an_operational_block() -> None:
    assert (
        watch_block_status_for_data_blocked(["QUOTE_STALE", "DATA_BLOCKED"])
        is EntryWatchStatus.BLOCKED_DATA
    )


def test_reconciliation_failure_is_an_operational_block() -> None:
    assert (
        watch_block_status_for_data_blocked(["RECONCILIATION_STALE"])
        is EntryWatchStatus.BLOCKED_OPERATIONAL
    )
