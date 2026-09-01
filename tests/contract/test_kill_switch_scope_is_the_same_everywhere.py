"""Every broker adapter must read the kill switch the same way.

P0-7 — halting the desk also disarming it — was found and closed once, in
`AlpacaPaperBroker`. The other two adapters kept refusing every order, so the
guarantee held for the broker in use and not for the broker in the vendor lock:
IBKR is the one going to production, and on it the switch would still have
refused the protective stop reconciliation was installing and the emergency
close that is the last way out.

The mock being wrong is why nothing caught it. A property asserted through the
mock could never pass, so "an exit works while halted" was never written down as
a test at all, and the fix stayed where it was first applied.

Parametrised over the adapters rather than asserted once, because the defect was
never that the rule was wrong. It was that the rule lived in one place and the
question gets asked in three.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from broker.ibkr.adapter import IBKRBroker
from broker.paper.mock import MockPaperBroker
from core.enums import IntentPurpose, OrderSide, OrderType, PositionStatus
from core.schemas import OrderRequest, Position
from risk.kill_switch import set_kill_switch

ADAPTERS = ("mock", "alpaca", "ibkr")

NEW_EXPOSURE = (IntentPurpose.ENTRY,)
REDUCES_RISK = (
    IntentPurpose.EXIT,
    IntentPurpose.PROTECTIVE_EXIT,
    IntentPurpose.EMERGENCY_EXIT,
)


def _request(purpose: IntentPurpose) -> OrderRequest:
    return OrderRequest(
        client_order_id=f"test-{uuid4().hex[:12]}",
        symbol="TEST",
        side=OrderSide.SELL if purpose is not IntentPurpose.ENTRY else OrderSide.BUY,
        order_type=OrderType.MARKET,
        qty=Decimal(10),
        reason="kill switch scope contract",
        purpose=purpose,
    )


def _place_order_source(adapter: str) -> str:
    """The refusal as written, so the assertion does not need a live venue."""
    import inspect

    if adapter == "mock":
        return inspect.getsource(MockPaperBroker.place_order)
    if adapter == "ibkr":
        return inspect.getsource(IBKRBroker.place_order)
    from broker.alpaca import AlpacaPaperBroker

    return inspect.getsource(AlpacaPaperBroker.place_order)


@pytest.fixture(autouse=True)
def _released_switch() -> Iterator[None]:
    set_kill_switch(False, actor="test", reason="setup")
    yield
    set_kill_switch(False, actor="test", reason="teardown")


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_the_refusal_is_scoped_to_new_exposure(adapter: str) -> None:
    """An adapter that ignores `purpose` refuses the way out of a bad position."""
    source = _place_order_source(adapter)

    assert "is_kill_switch_on()" in source, f"{adapter} does not consult the kill switch"
    assert "reduces_risk" in source, (
        f"{adapter} refuses every order while halted, which disarms the desk "
        "it is meant to protect (P0-7)"
    )


@pytest.mark.parametrize("purpose", REDUCES_RISK)
@pytest.mark.asyncio
async def test_a_halted_mock_still_places_what_defends_a_position(
    purpose: IntentPurpose,
) -> None:
    broker = MockPaperBroker()
    broker.positions.append(
        Position(
            id=uuid4(),
            symbol="TEST",
            qty=Decimal(10),
            avg_entry=Decimal(100),
            status=PositionStatus.OPEN,
            opened_at=datetime.now(UTC),
        )
    )
    set_kill_switch(True, actor="test", reason="halted")

    record = await broker.place_order(_request(purpose))

    assert record.broker_order_id


@pytest.mark.parametrize("purpose", NEW_EXPOSURE)
@pytest.mark.asyncio
async def test_a_halted_mock_still_refuses_new_exposure(purpose: IntentPurpose) -> None:
    broker = MockPaperBroker()
    set_kill_switch(True, actor="test", reason="halted")

    with pytest.raises(RuntimeError, match="KILL_SWITCH"):
        await broker.place_order(_request(purpose))


@pytest.mark.asyncio
async def test_an_unlabelled_order_is_treated_as_new_exposure() -> None:
    """Forgetting the purpose must fail in the safe direction."""
    broker = MockPaperBroker()
    request = OrderRequest(
        client_order_id=f"test-{uuid4().hex[:12]}",
        symbol="TEST",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        qty=Decimal(10),
        reason="kill switch scope contract",
    )
    set_kill_switch(True, actor="test", reason="halted")

    with pytest.raises(RuntimeError, match="KILL_SWITCH"):
        await broker.place_order(request)
