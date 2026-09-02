"""
Both intent stores, one set of guarantees.

The in-memory store is what most tests use; the SQL store is what production
uses. Running the same assertions against both is the only way the fast tests
say anything about the slow path.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine

from core.enums import IntentStatus, OrderSide, OrderType
from trading.intents import MemoryOrderIntentStore, OrderIntentStore, OrderIntentStorePort
from trading.order_intent import IllegalTransition, OrderIntent


@pytest.fixture(params=["memory", "sql"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[OrderIntentStorePort]:
    if request.param == "memory":
        yield MemoryOrderIntentStore()
        return
    engine = create_engine(f"sqlite:///{tmp_path / 'intents.db'}", future=True)
    yield OrderIntentStore(engine)
    engine.dispose()


def _intent(**overrides: Any) -> OrderIntent:
    rid = uuid4()
    base: dict[str, Any] = {
        "idempotency_key": "entry:store-test:0",
        "broker": "MockPaperBroker",
        "broker_account_id": "mock-paper-account",
        "broker_environment": "paper",
        "symbol": "AAPL",
        "side": OrderSide.BUY,
        "requested_qty": Decimal(10),
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal("100.50"),
        "opportunity_id": uuid4(),
        "approval_admission_record_id": uuid4(),
        "geometry_hash": "intent-store-test",
        "request_id": rid,
        "request_fingerprint": "a" * 32,
    }
    base.update(overrides)
    return OrderIntent(**base)


def test_the_same_key_never_creates_a_second_intent(store: OrderIntentStorePort) -> None:
    first, created_first = store.create_or_get(_intent())
    second, created_second = store.create_or_get(_intent())

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert store.get_by_key("entry:store-test:0") is not None


def test_a_round_trip_preserves_decimals_and_ids(store: OrderIntentStorePort) -> None:
    """Prices go through JSON in the SQL store; they must come back exact."""
    original = _intent(stop_price=Decimal("95.25"))
    store.create_or_get(original)

    loaded = store.get(original.id)

    assert loaded is not None
    assert loaded.limit_price == Decimal("100.50")
    assert loaded.stop_price == Decimal("95.25")
    assert loaded.opportunity_id == original.opportunity_id
    assert loaded.side is OrderSide.BUY


def test_transitions_are_validated_against_persisted_state(
    store: OrderIntentStorePort,
) -> None:
    intent, _ = store.create_or_get(_intent())
    store.transition(intent.id, IntentStatus.SUBMITTING, client_order_id="traido-e-x")

    # The caller still holds the stale CREATED copy; the store must not care.
    with pytest.raises(IllegalTransition):
        store.transition(intent.id, IntentStatus.SUBMITTING)

    assert store.get(intent.id).client_order_id == "traido-e-x"


def test_unresolved_intents_are_discoverable_by_symbol(store: OrderIntentStorePort) -> None:
    settled, _ = store.create_or_get(_intent(idempotency_key="entry:a:0", symbol="MSFT"))
    store.transition(settled.id, IntentStatus.REJECTED)
    store.create_or_get(
        _intent(idempotency_key="entry:b:0", symbol="AAPL", status=IntentStatus.UNKNOWN)
    )

    assert store.unresolved_symbols() == {"AAPL"}
    assert [i.symbol for i in store.list_unresolved()] == ["AAPL"]


def test_attempts_are_countable_by_key_prefix(store: OrderIntentStorePort) -> None:
    """Attempt numbering is derived from storage, so a restart recomputes it."""
    opportunity = uuid4()
    for attempt in range(3):
        store.create_or_get(
            _intent(idempotency_key=f"entry:{opportunity}:{attempt}", opportunity_id=opportunity)
        )

    assert len(store.list_by_key_prefix(f"entry:{opportunity}:")) == 3
    assert store.list_by_key_prefix("entry:someone-else:") == []


def test_update_fields_records_broker_news_without_moving_state(
    store: OrderIntentStorePort,
) -> None:
    intent, _ = store.create_or_get(_intent())
    store.transition(intent.id, IntentStatus.SUBMITTING)
    store.transition(intent.id, IntentStatus.SUBMITTED, broker_order_id="abc")

    store.update_fields(intent.id, filled_qty=Decimal(4), last_broker_state="partially_filled")

    updated = store.get(intent.id)
    assert updated.status is IntentStatus.SUBMITTED
    assert updated.filled_qty == Decimal(4)
    assert updated.last_broker_state == "partially_filled"


def test_a_missing_intent_is_an_error_not_a_silent_no_op(store: OrderIntentStorePort) -> None:
    with pytest.raises(ValueError, match="order_intent_not_found"):
        store.transition(uuid4(), IntentStatus.SUBMITTING)
