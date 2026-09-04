"""One place where the execution service is assembled.

Every route used to build its own, and the two that authorize a trade both left
out `market_data`. That argument is optional, so nothing complained — and the
liquidity gate, which is the only thing measuring spread, average dollar volume,
price floor, participation and expected slippage, quietly never ran on the live
desk. The gate was written, tested and documented as enforced; it was simply
never handed the port it measures with.

A constructor argument that silently disables a capital gate when omitted is a
defect waiting for the next call site, so the routes no longer call the
constructor. They call this, and a test refuses to let them go back.
"""

from __future__ import annotations

from collections.abc import Coroutine
from typing import Any

from broker.factory import create_broker
from core.audit import create_audit
from core.config import get_settings
from core.ports import AuditPort, BrokerPort
from market_data.factory import create_market_data_port
from trading.entry_policy import get_entry_thresholds
from trading.execution import ExecutionService
from trading.exits import EXITS
from trading.gates import LiquidityPolicy
from trading.opportunities import OPPORTUNITIES


def build_execution_service(
    *, broker: BrokerPort | None = None, audit: AuditPort | None = None
) -> ExecutionService:
    """The execution service as the desk runs it, with every gate armed.

    `broker` and `audit` may be passed by a caller that already holds them — the
    desk payload builds both to render itself and should not open a second
    broker session to reconcile. Nothing else is overridable, because the rest
    is what makes the gates work.
    """
    settings = get_settings()
    entry_th = get_entry_thresholds()
    return ExecutionService(
        broker=broker if broker is not None else create_broker(settings),
        audit=audit if audit is not None else create_audit(),
        store=OPPORTUNITIES,
        exit_store=EXITS,
        market_data=create_market_data_port(settings),
        liquidity_policy=LiquidityPolicy(
            max_spread_bps=entry_th.max_spread_bps,
            max_quote_age_sec=entry_th.quote_max_age_sec,
        ),
    )


def build_exit_assessment() -> Coroutine[Any, Any, Any]:
    """One pass of the position agent, wired the way the desk wires everything else.

    Returned unawaited, and built here for the same reason as the reconciliation
    pass: the loop stays out of the vendor-construction business, and the
    background pass and any route-driven one share identical wiring.
    """
    from trading.exits import refresh_exit_proposals

    settings = get_settings()
    return refresh_exit_proposals(create_broker(settings), create_market_data_port(settings))


def build_reconcile_pass() -> Coroutine[Any, Any, Any]:
    """One reconciliation pass, wired the way the desk wires everything else.

    Returned unawaited: the supervisor decides whether this pass runs at all, or
    whether a caller joins one already in flight. Building it here rather than
    in `api/main.py` keeps the background loop out of the vendor-construction
    business, and means the loop and the dashboard route reconcile through
    identical wiring — including the same `build_execution_service`, so the
    liquidity gate cannot be armed on one path and absent on the other.
    """
    from trading.reconcile import reconcile_positions

    settings = get_settings()
    broker = create_broker(settings)
    audit = create_audit()
    return reconcile_positions(
        broker,
        audit,
        execution=build_execution_service(broker=broker, audit=audit),
    )
