"""Explicit owner-selected Paper exit policy, including existing positions."""

from typing import Any
from uuid import UUID

from core.config import get_settings
from core.enums import BrokerEnvironment

PROTECTED_INTRADAY = "protected_intraday_v1"


def manual_target_exits(payload: dict[str, Any] | None = None) -> bool:
    settings = get_settings()
    if settings.broker_env is BrokerEnvironment.PAPER:
        from trading.auto_stop_policy import get_override

        # An explicit OFF applies to every open/new position, including rows
        # whose original approval froze protected_intraday_v1. Timers and the
        # execution boundary must agree with the stop switch after a restart.
        if get_override() is False:
            return True
    if payload is not None and payload.get("position_exit_policy") == PROTECTED_INTRADAY:
        return False
    return (
        settings.broker_env == BrokerEnvironment.PAPER
        and settings.paper_exit_policy == "manual_target"
    )


def manual_target_position(symbol: str, position_id: UUID | None = None) -> bool:
    from trading.ledger import LEDGER

    row = LEDGER.get(position_id) if position_id is not None else LEDGER.find_open_by_symbol(symbol)
    return manual_target_exits(row.payload if row is not None else None)


def automatic_stops_enabled(payload: dict[str, Any] | None = None) -> bool:
    """OFF also selects target/manual-only exits; ON restores stop protection."""
    if get_settings().broker_env is BrokerEnvironment.PAPER:
        from trading.auto_stop_policy import get_override

        selected = get_override()
        if selected is not None:
            return selected
    return not manual_target_exits(payload)


def automatic_stops_for_position(
    symbol: str, position_id: UUID | None = None, *, protected_entry: bool = False
) -> bool:
    from trading.ledger import LEDGER

    row = LEDGER.get(position_id) if position_id is not None else LEDGER.find_open_by_symbol(symbol)
    payload = row.payload if row is not None else None
    if protected_entry:
        payload = {"position_exit_policy": PROTECTED_INTRADAY}
    return automatic_stops_enabled(payload)
