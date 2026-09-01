"""
Order lifecycle rules.

These tests are about one thing: the states an order may legally move between,
and the refusal to invent a state we did not observe.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import pairwise

import pytest

from core.enums import IntentStatus, OrderSide, OrderStatus, OrderType
from trading.order_intent import (
    ALLOWED_TRANSITIONS,
    IN_FLIGHT,
    TERMINAL,
    UNRESOLVED,
    IllegalTransition,
    OrderIntent,
    assert_transition,
    can_transition,
    entry_idempotency_key,
    intent_status_for,
)


def _intent(**overrides: object) -> OrderIntent:
    base: dict[str, object] = {
        "idempotency_key": "entry:test:0",
        "broker": "MockPaperBroker",
        "symbol": "AAPL",
        "side": OrderSide.BUY,
        "requested_qty": Decimal(10),
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal("100.00"),
    }
    base.update(overrides)
    return OrderIntent(**base)  # type: ignore[arg-type]


# ── Happy path ───────────────────────────────────────────────────────────────


def test_the_normal_entry_lifecycle_is_permitted() -> None:
    path = [
        IntentStatus.CREATED,
        IntentStatus.SUBMITTING,
        IntentStatus.SUBMITTED,
        IntentStatus.ACKNOWLEDGED,
        IntentStatus.PARTIALLY_FILLED,
        IntentStatus.FILLED,
    ]
    for current, target in pairwise(path):
        assert can_transition(current, target), f"{current} -> {target} should be allowed"


def test_a_cancel_can_lose_the_race_with_a_fill() -> None:
    """Real brokers fill orders we have already asked to cancel."""
    assert can_transition(IntentStatus.CANCEL_PENDING, IntentStatus.FILLED)
    assert can_transition(IntentStatus.CANCEL_PENDING, IntentStatus.PARTIALLY_FILLED)


# ── Illegal transitions ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (IntentStatus.CREATED, IntentStatus.FILLED),
        (IntentStatus.CREATED, IntentStatus.SUBMITTED),
        (IntentStatus.FILLED, IntentStatus.CANCELED),
        (IntentStatus.CANCELED, IntentStatus.FILLED),
        (IntentStatus.REJECTED, IntentStatus.SUBMITTED),
        (IntentStatus.EXPIRED, IntentStatus.PARTIALLY_FILLED),
        (IntentStatus.FILLED, IntentStatus.UNKNOWN),
    ],
)
def test_illegal_transitions_are_refused(current: IntentStatus, target: IntentStatus) -> None:
    assert not can_transition(current, target)
    with pytest.raises(IllegalTransition):
        assert_transition(current, target)


def test_every_state_is_covered_by_the_transition_table() -> None:
    """A state missing from the table would silently allow nothing at all."""
    assert set(ALLOWED_TRANSITIONS) == set(IntentStatus)


def test_terminal_states_are_final() -> None:
    for state in TERMINAL:
        assert ALLOWED_TRANSITIONS[state] == frozenset()


# ── UNKNOWN ──────────────────────────────────────────────────────────────────


def test_unknown_is_not_terminal_and_can_recover_in_any_direction() -> None:
    """UNKNOWN means "not yet established", so every real outcome stays reachable."""
    assert IntentStatus.UNKNOWN not in TERMINAL
    assert IntentStatus.UNKNOWN in UNRESOLVED
    for outcome in (
        IntentStatus.FILLED,
        IntentStatus.PARTIALLY_FILLED,
        IntentStatus.CANCELED,
        IntentStatus.REJECTED,
        IntentStatus.EXPIRED,
    ):
        assert can_transition(IntentStatus.UNKNOWN, outcome)


def test_ambiguous_states_reach_unknown_but_settled_ones_do_not() -> None:
    for state in IN_FLIGHT - {IntentStatus.UNKNOWN}:
        assert can_transition(state, IntentStatus.UNKNOWN), state
    for state in TERMINAL:
        assert not can_transition(state, IntentStatus.UNKNOWN), state


# ── Broker status normalization ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("broker_status", "expected"),
    [
        (OrderStatus.SUBMITTED, IntentStatus.SUBMITTED),
        (OrderStatus.ACCEPTED, IntentStatus.ACKNOWLEDGED),
        (OrderStatus.PARTIAL, IntentStatus.PARTIALLY_FILLED),
        (OrderStatus.FILLED, IntentStatus.FILLED),
        (OrderStatus.CANCELED, IntentStatus.CANCELED),
        (OrderStatus.REJECTED, IntentStatus.REJECTED),
        (OrderStatus.EXPIRED, IntentStatus.EXPIRED),
    ],
)
def test_broker_states_map_onto_domain_states(
    broker_status: OrderStatus, expected: IntentStatus
) -> None:
    assert intent_status_for(broker_status, None) is expected


@pytest.mark.parametrize("broker_status", [OrderStatus.CANCELED, OrderStatus.EXPIRED])
def test_a_cancelled_order_that_filled_something_is_not_merely_cancelled(
    broker_status: OrderStatus,
) -> None:
    """Those shares are a live position; calling this CANCELED would lose them."""
    assert intent_status_for(broker_status, Decimal(3)) is IntentStatus.PARTIALLY_FILLED


# ── Resubmission ─────────────────────────────────────────────────────────────


def test_only_an_untransmitted_intent_may_be_submitted() -> None:
    assert _intent().may_resubmit

    for state in IN_FLIGHT | TERMINAL:
        assert not _intent(status=state).may_resubmit, state

    # Even in CREATED, a broker id means something was sent.
    assert not _intent(broker_order_id="abc").may_resubmit


def test_idempotency_keys_are_stable_per_attempt() -> None:
    from uuid import uuid4

    opportunity = uuid4()
    assert entry_idempotency_key(opportunity, 0) == entry_idempotency_key(opportunity, 0)
    assert entry_idempotency_key(opportunity, 0) != entry_idempotency_key(opportunity, 1)
    assert entry_idempotency_key(opportunity, 0) != entry_idempotency_key(uuid4(), 0)
