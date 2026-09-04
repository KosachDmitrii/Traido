"""Guards for switching the execution broker at runtime."""

from __future__ import annotations

from core.enums import IntentStatus
from trading.intents import INTENTS
from trading.ledger import LEDGER


def broker_switch_blocked_reason() -> str | None:
    """Why the operator cannot change execution venue right now.

    Open ledger rows or unresolved intents (including UNKNOWN) mean broker truth
    is still in play — swapping the adapter would strand that state.
    """
    open_rows = LEDGER.get_open()
    if open_rows:
        symbols = ",".join(sorted({r.symbol for r in open_rows})[:6])
        return f"open_positions:{symbols}"

    unresolved = INTENTS.list_unresolved()
    if unresolved:
        unknown = [i for i in unresolved if i.status is IntentStatus.UNKNOWN]
        if unknown:
            return f"unknown_intents:{len(unknown)}"
        return f"open_intents:{len(unresolved)}"
    return None
