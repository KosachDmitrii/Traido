"""
One lifecycle contract, two brokers.

Every test here runs against both the Alpaca and the IBKR adapter, each backed
by a fake that speaks its own vendor dialect. The assertions are written purely
in Traido's vocabulary: if a test passes for one adapter and fails for the
other, the adapters disagree about what a broker event means, and that
disagreement would otherwise surface as a position nobody expected.

Alpaca must keep passing this suite for as long as it is the live path. IBKR
must pass it before it can replace Alpaca.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from broker.interface import BrokerRejection, BrokerUnreachable
from core.enums import IntentPurpose, IntentStatus, OrderSide, OrderStatus, OrderType
from core.schemas import OrderRequest
from tests.contract.fakes import alpaca_adapter, ibkr_adapter
from trading.order_intent import OrderIntent, intent_status_for, locate_broker_order

ADAPTERS = [
    pytest.param(alpaca_adapter, id="alpaca"),
    pytest.param(ibkr_adapter, id="ibkr"),
]

pytestmark = pytest.mark.asyncio


def _buy(qty: str = "10", client_order_id: str = "traido-e-contract") -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id,
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal(qty),
        limit_price=Decimal("100.00"),
        reason="broker contract suite",
    )


def _sell_stop(qty: str = "10") -> OrderRequest:
    return OrderRequest(
        client_order_id="traido-s-contract",
        symbol="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        qty=Decimal(qty),
        stop_price=Decimal("95.00"),
        reason="protective stop",
    )


# ── Submission ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_submit_accepted_is_normalized(adapter: Any) -> None:
    broker, _ = adapter(fill_ratio=0.0)
    record = await broker.place_order(_buy())

    assert record.status is OrderStatus.ACCEPTED
    assert record.broker_order_id
    assert record.client_order_id == "traido-e-contract"
    assert record.symbol == "AAPL"
    assert record.side is OrderSide.BUY


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_submit_rejected_raises_a_rejection_not_an_ambiguity(adapter: Any) -> None:
    """The distinction is load-bearing: a rejection is safe to retry, silence is not."""
    broker, _ = adapter(reject=True)

    with pytest.raises(BrokerRejection):
        await broker.place_order(_buy())


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_full_fill_is_normalized(adapter: Any) -> None:
    broker, _ = adapter(fill_ratio=1.0)
    record = await broker.place_order(_buy())

    assert record.status is OrderStatus.FILLED
    assert record.filled_qty == Decimal(10)
    assert record.filled_avg_price == Decimal("100.00")


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_partial_fill_is_normalized(adapter: Any) -> None:
    broker, _ = adapter(fill_ratio=0.4)
    record = await broker.place_order(_buy())

    assert record.status is OrderStatus.PARTIAL
    assert record.filled_qty == Decimal(4)


# ── Cancellation ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_cancel_before_fill(adapter: Any) -> None:
    broker, _ = adapter(fill_ratio=0.0)
    submitted = await broker.place_order(_buy())

    cancelled = await broker.cancel_order(submitted.broker_order_id or "")

    assert cancelled.status is OrderStatus.CANCELED
    assert not cancelled.filled_qty


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_cancel_after_partial_fill_keeps_the_fill(adapter: Any) -> None:
    """Cancelling a partly filled order kills the remainder, not the shares."""
    broker, _ = adapter(fill_ratio=0.4)
    submitted = await broker.place_order(_buy())

    await broker.cancel_order(submitted.broker_order_id or "")
    final = await broker.get_order(submitted.broker_order_id or "")

    assert final.filled_qty == Decimal(4)
    assert final.status is OrderStatus.CANCELED
    # And the domain refuses to call that "cancelled": 4 shares are a position.
    assert intent_status_for(final.status, final.filled_qty) is IntentStatus.PARTIALLY_FILLED


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_position_appears_after_fill(adapter: Any) -> None:
    broker, _ = adapter(fill_ratio=1.0)
    await broker.place_order(_buy())

    positions = await broker.list_positions()

    assert [p.symbol for p in positions] == ["AAPL"]
    assert positions[0].qty == Decimal(10)


# ── Recovery ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_get_order_recovers_state_after_a_lost_response(adapter: Any) -> None:
    """The core restart primitive: an order id is enough to re-learn the truth."""
    broker, _ = adapter(fill_ratio=1.0)
    submitted = await broker.place_order(_buy())

    recovered = await broker.get_order(submitted.broker_order_id or "")

    assert recovered.broker_order_id == submitted.broker_order_id
    assert recovered.status is OrderStatus.FILLED
    assert recovered.filled_qty == Decimal(10)


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("fill_ratio", [0.0, 1.0], ids=["resting", "already-filled"])
async def test_order_is_findable_by_client_id_after_a_lost_reply(
    adapter: Any, fill_ratio: float
) -> None:
    """Recovery must work even once the order has filled.

    A filled order is not an open order, so a lookup restricted to the open
    book would report "not found" for the single most dangerous case: an entry
    that executed while we were not listening.
    """
    broker, _ = adapter(fill_ratio=fill_ratio)
    await broker.place_order(_buy(client_order_id="traido-e-lost-reply"))

    intent = OrderIntent(
        idempotency_key="entry:contract:0",
        broker="contract",
        symbol="AAPL",
        side=OrderSide.BUY,
        requested_qty=Decimal(10),
        order_type=OrderType.LIMIT,
        client_order_id="traido-e-lost-reply",
    )
    found = await locate_broker_order(broker, intent)

    assert found is not None
    assert found.client_order_id == "traido-e-lost-reply"


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_client_id_lookup_of_an_unknown_order_returns_none(adapter: Any) -> None:
    broker, _ = adapter()

    assert await broker.find_order_by_client_id("never-sent") is None


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_unknown_broker_state_is_not_reported_as_absence(adapter: Any) -> None:
    """A broker with no trace of our order must yield None, never a fake 'cancelled'."""
    broker, _ = adapter(fill_ratio=1.0, forget_orders=True)
    submitted = await broker.place_order(_buy(client_order_id="traido-e-vanished"))

    intent = OrderIntent(
        idempotency_key="entry:contract:0",
        broker="contract",
        symbol="AAPL",
        side=OrderSide.BUY,
        requested_qty=Decimal(10),
        order_type=OrderType.LIMIT,
        broker_order_id=submitted.broker_order_id,
        client_order_id="traido-e-vanished",
    )

    assert await locate_broker_order(broker, intent) is None
    assert intent_status_for(OrderStatus.SUBMITTED, None) is not IntentStatus.CANCELED


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_reading_a_nonexistent_order_is_ambiguous_not_a_rejection(adapter: Any) -> None:
    broker, _ = adapter()

    with pytest.raises((BrokerUnreachable, RuntimeError)) as excinfo:
        await broker.get_order("no-such-order")

    assert not isinstance(excinfo.value, BrokerRejection)


# ── Protective orders ────────────────────────────────────────────────────────


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_protective_stop_rests_in_the_open_order_book(adapter: Any) -> None:
    """Reconciliation detects a missing stop by looking here, so it must appear."""
    broker, _ = adapter(fill_ratio=1.0)
    await broker.place_order(_buy())
    stop = await broker.place_order(_sell_stop())

    open_ids = {o.broker_order_id for o in await broker.list_open_orders()}

    assert stop.broker_order_id in open_ids
    assert stop.stop_price == Decimal("95.00")


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_missing_protective_stop_is_visible_as_absence(adapter: Any) -> None:
    broker, _ = adapter(fill_ratio=1.0)
    await broker.place_order(_buy())
    stop = await broker.place_order(_sell_stop())
    await broker.cancel_order(stop.broker_order_id or "")

    open_ids = {o.broker_order_id for o in await broker.list_open_orders()}

    assert stop.broker_order_id not in open_ids


# ── Exit lifecycle ───────────────────────────────────────────────────────────
#
# Stage 7.1. Exits get the same treatment as entries because they fail the same
# ways, and an exit that silently duplicates is a short position.


def _sell_market(qty: str = "10", client_order_id: str = "traido-x-contract") -> OrderRequest:
    return OrderRequest(
        client_order_id=client_order_id,
        symbol="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        qty=Decimal(qty),
        reason="contract exit",
    )


async def _with_position(adapter: Any, **kwargs: Any) -> tuple[Any, Any]:
    broker, backend = adapter(fill_ratio=1.0, **kwargs)
    await broker.place_order(_buy())
    return broker, backend


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_a_full_exit_removes_the_position(adapter: Any) -> None:
    broker, _ = await _with_position(adapter)

    record = await broker.place_order(_sell_market())

    assert record.status is OrderStatus.FILLED
    assert record.filled_qty == Decimal(10)
    assert await broker.list_positions() == []


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_a_partial_exit_leaves_the_remainder_at_the_broker(adapter: Any) -> None:
    broker, backend = await _with_position(adapter)
    backend.fill_ratio = 0.3

    record = await broker.place_order(_sell_market())

    assert record.status is OrderStatus.PARTIAL
    assert record.filled_qty == Decimal(3)
    assert (await broker.list_positions())[0].qty == Decimal(7)


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_a_broker_cancel_after_a_partial_exit_keeps_the_shares_sold(adapter: Any) -> None:
    """3 shares left the account. Cancelling the rest cannot bring them back."""
    broker, backend = await _with_position(adapter)
    backend.fill_ratio = 0.3
    submitted = await broker.place_order(_sell_market())

    await broker.cancel_order(submitted.broker_order_id or "")
    final = await broker.get_order(submitted.broker_order_id or "")

    assert final.filled_qty == Decimal(3)
    assert intent_status_for(final.status, final.filled_qty) is IntentStatus.PARTIALLY_FILLED
    assert (await broker.list_positions())[0].qty == Decimal(7)


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("fill_ratio", [0.0, 0.3, 1.0], ids=["resting", "partial", "filled"])
async def test_an_exit_is_findable_by_client_id_after_a_lost_reply(
    adapter: Any, fill_ratio: float
) -> None:
    """The primitive that stops a retry becoming a second sale."""
    broker, backend = await _with_position(adapter)
    backend.fill_ratio = fill_ratio
    await broker.place_order(_sell_market(client_order_id="traido-x-lost"))

    found = await locate_broker_order(broker, _exit_intent("traido-x-lost"))

    assert found is not None
    assert found.side is OrderSide.SELL
    assert found.client_order_id == "traido-x-lost"


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_an_exit_the_broker_forgot_is_unresolved_not_absent(adapter: Any) -> None:
    broker, backend = await _with_position(adapter)
    backend.forget_orders = True
    await broker.place_order(_sell_market(client_order_id="traido-x-vanished"))

    assert await locate_broker_order(broker, _exit_intent("traido-x-vanished")) is None


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_resizing_protection_leaves_exactly_one_resting_stop(adapter: Any) -> None:
    """After a partial exit the old stop must go and a smaller one take its place."""
    broker, backend = await _with_position(adapter)
    original = await broker.place_order(_sell_stop("10"))
    backend.fill_ratio = 0.3
    await broker.place_order(_sell_market())

    await broker.cancel_order(original.broker_order_id or "")
    resized = await broker.place_order(
        OrderRequest(
            client_order_id="traido-s-resized",
            symbol="AAPL",
            side=OrderSide.SELL,
            order_type=OrderType.STOP,
            qty=Decimal(7),
            stop_price=Decimal("95.00"),
            reason="resize after partial exit",
        )
    )

    resting = [o for o in await broker.list_open_orders() if o.order_type is OrderType.STOP]
    assert [o.broker_order_id for o in resting] == [resized.broker_order_id]
    assert resting[0].qty == Decimal(7)
    assert resting[0].qty <= (await broker.list_positions())[0].qty


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_an_orphan_broker_position_is_visible(adapter: Any) -> None:
    """Reconciliation can only block what it can see."""
    broker, _ = await _with_position(adapter)

    positions = await broker.list_positions()

    assert [p.symbol for p in positions] == ["AAPL"]
    assert positions[0].qty == Decimal(10)


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_a_position_the_broker_still_holds_is_reported_as_held(adapter: Any) -> None:
    """'Local says closed' is never evidence; only the broker's answer counts."""
    broker, backend = await _with_position(adapter)
    backend.fill_ratio = 0.5
    await broker.place_order(_sell_market())

    assert (await broker.list_positions())[0].qty == Decimal(5)


def _exit_intent(client_order_id: str) -> OrderIntent:
    return OrderIntent(
        idempotency_key=f"exit:contract:{client_order_id}",
        purpose=IntentPurpose.EXIT,
        broker="contract",
        symbol="AAPL",
        side=OrderSide.SELL,
        requested_qty=Decimal(10),
        order_type=OrderType.MARKET,
        client_order_id=client_order_id,
    )


# ── Idempotency of reads ─────────────────────────────────────────────────────


@pytest.mark.parametrize("adapter", ADAPTERS)
async def test_repeated_reads_are_stable(adapter: Any) -> None:
    """Reconciliation runs on a loop; reading must not change anything."""
    broker, backend = adapter(fill_ratio=0.4)
    submitted = await broker.place_order(_buy())

    first = await broker.get_order(submitted.broker_order_id or "")
    second = await broker.get_order(submitted.broker_order_id or "")
    third = await broker.get_order(submitted.broker_order_id or "")

    assert first.status is second.status is third.status
    assert first.filled_qty == second.filled_qty == third.filled_qty
    assert backend.submit_count == 1
