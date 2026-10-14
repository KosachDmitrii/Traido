"""Explicit owner-selected Paper exit policy, including existing positions."""

from typing import Any
from uuid import UUID

from core.config import get_settings
from core.enums import BrokerEnvironment

PROTECTED_INTRADAY = "protected_intraday_v1"


def manual_target_exits(payload: dict[str, Any] | None = None) -> bool:
    if payload is not None and payload.get("position_exit_policy") == PROTECTED_INTRADAY:
        return False
    settings = get_settings()
    return (
        settings.broker_env == BrokerEnvironment.PAPER
        and settings.paper_exit_policy == "manual_target"
    )


def manual_target_position(symbol: str, position_id: UUID | None = None) -> bool:
    from trading.ledger import LEDGER

    row = LEDGER.get(position_id) if position_id is not None else LEDGER.find_open_by_symbol(symbol)
    return manual_target_exits(row.payload if row is not None else None)
