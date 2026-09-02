"""Stable domain errors for the ApprovalAdmission capital path."""

from __future__ import annotations


class ApprovalDomainError(RuntimeError):
    """Base: maps to a stable HTTP status + code."""

    code: str = "APPROVAL_ERROR"
    http_status: int = 409

    def __init__(self, detail: str = "") -> None:
        self.detail = detail or self.code
        super().__init__(f"{self.code}:{self.detail}")


class StaleDecisionError(ApprovalDomainError):
    code = "STALE_DECISION"
    http_status = 409


class IdempotencyConflictError(ApprovalDomainError):
    code = "IDEMPOTENCY_CONFLICT"
    http_status = 409


class EntryInFlightError(ApprovalDomainError):
    code = "ENTRY_IN_FLIGHT"
    http_status = 409


class DataBlockedError(ApprovalDomainError):
    code = "DATA_BLOCKED"
    http_status = 422


class NoTradeError(ApprovalDomainError):
    code = "NO_TRADE"
    http_status = 422


class WaitError(ApprovalDomainError):
    code = "WAIT"
    http_status = 422
