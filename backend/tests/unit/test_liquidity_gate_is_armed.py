"""The liquidity gate has to be reachable, not merely written.

pytestmark = pytest.mark.usefixtures("capital_path_ready")

It existed, was fail-closed, was covered by `test_gates.py`, and was described
in `ARCHITECTURE.md` as enforced inside the execution service. None of that was
false about the gate. It was false about the desk: both routes that authorize a
trade built `ExecutionService` without `market_data`, that argument is optional,
and `_entry_gates` returned "no failure" when it was missing. So every approve
on the running desk skipped spread, average dollar volume, the price floor,
participation and the slippage estimate — the only place any of them is checked.

Two failures had to line up, so this pins both. A service with no data port must
refuse rather than shrug, and the routes must not be able to build one.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from api.deps import build_execution_service
from core.audit import InMemoryAudit
from core.enums import TradeAction, TradingMode, UserDecision
from core.schemas import TradeCandidate
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskEngine
from tests.support import CLEARED_EARNINGS
from trading.execution import ExecutionService
from trading.opportunities import MemoryOpportunityStore

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("capital_path_ready")]

REPO = Path(__file__).resolve().parents[2]


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="AAPL",
        action=TradeAction.BUY,
        entry=Decimal(100),
        stop=Decimal(95),
        target=Decimal(115),
        confidence=0.8,
        risk_reward=3.0,
        reasons=["liquidity wiring test"],
        strategy_version="test@1",
        pipeline_run_id=uuid4(),
    )


# ── The gate refuses when it cannot measure ──────────────────────────────────


@pytest.mark.asyncio
async def test_a_service_with_no_market_data_refuses_the_entry() -> None:
    """An unmeasured spread is not a narrow one.

    The same rule the earnings calendar follows, and for the same reason: the
    only thing separating "checked, fine" from "never checked" is whether the
    code says so.
    """
    from broker.paper.mock import MockPaperBroker

    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    service = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        market_data=None,
        require_rth=False,
    )

    with pytest.raises(RuntimeError) as err:
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )

    assert "MARKET_DATA_NOT_CONFIGURED" in str(err.value)
    assert "LIQUIDITY_GATE_REJECTED" in str(err.value)


def test_the_desk_builds_a_service_that_can_measure_liquidity() -> None:
    """The wiring itself, asserted — this is the half that was actually broken."""
    service = build_execution_service()

    assert service.market_data is not None, "the liquidity gate has nothing to measure"
    assert service.quotes is not None, "the spread check has no top of book"


# ── And the wiring cannot be bypassed again ──────────────────────────────────


def test_no_route_constructs_the_execution_service_directly() -> None:
    """A route that builds its own can omit a gate without anything complaining.

    Source-scanned rather than mocked, for the same reason `place_order` is: the
    property is "nowhere in this directory", and no test double can show that.
    """
    offenders = []
    for path in sorted((REPO / "api").rglob("*.py")):
        if path.name == "deps.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name == "ExecutionService":
                    offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")

    assert not offenders, (
        "build the service through api.deps.build_execution_service, "
        f"or a gate can go unarmed again: {offenders}"
    )
