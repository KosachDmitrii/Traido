"""Tests 10–18: what happens once the gates let an order through.

The gate tests assert absence — nothing reached the broker. These assert the
harder thing: that exactly the right amount reached it, exactly once, and that
the local book agrees with what the venue actually did.

The recurring failure mode in execution code is not "the order did not go". It
is "the order went twice", "the fill was forgotten", or "the position was left
naked while the log said success". Each of those is a test here.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

from core.enums import IntentPurpose, IntentStatus, OrderSide


def _intents_with_prefix(prefix: str, symbol: str = "AAPL"):
    """Read the durable intents the way reconciliation does.

    Through the real store rather than a captured list: an intent that exists
    only in a test's memory would prove nothing about surviving a restart.
    """
    from trading.intents import INTENTS

    return [i for i in INTENTS.list_by_key_prefix(prefix) if i.symbol == symbol.upper()]


def _exit_intents() -> list:
    from trading.intents import INTENTS

    return [i for i in INTENTS.list_by_key_prefix("") if i.purpose is IntentPurpose.EXIT]


def _entry_intents(symbol: str = "AAPL"):
    return _intents_with_prefix("entry:", symbol)


def _open_positions():
    from trading.ledger import LEDGER

    return LEDGER.get_open()


def _audit_event_types() -> set[str]:
    """Every audit event written to this test's database."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from database.models.desk import AuditEventRow
    from database.session import get_sync_engine, init_db

    with Session(init_db(get_sync_engine())) as session:
        return {row for row in session.scalars(select(AuditEventRow.event_type))}


# ── Test 10 · the happy path, exactly once ───────────────────────────────────


def test_10_a_clean_approval_produces_one_intent_and_one_entry_order(desk) -> None:
    opp = desk.offer("AAPL")

    response = desk.approve(opp.id)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "executed"

    entries = [m for m in desk.backend.placed if m.side == "buy"]
    assert len(entries) == 1, f"expected exactly one entry order, got {desk.backend.placed}"
    assert len(_entry_intents()) == 1, "one approval, one durable intent"

    stops = desk.backend.placed_of_type("stop")
    assert len(stops) == 1, "the filled position must be protected"
    assert stops[0].qty == entries[0].qty, "protection must cover the whole position"

    positions = _open_positions()
    assert len(positions) == 1
    assert positions[0].symbol == "AAPL"
    assert Decimal(str(positions[0].qty)) == Decimal(entries[0].qty)


def test_10b_the_entry_client_id_is_derived_from_the_durable_intent(desk) -> None:
    """The handle that makes recovery possible, asserted rather than assumed.

    If the client id were random, a lost reply would leave nothing to search
    the venue with, and test 12 could not work at all.
    """
    opp = desk.offer("AAPL")
    desk.approve(opp.id)

    intent = _entry_intents()[0]
    entry = next(m for m in desk.backend.placed if m.side == "buy")
    assert entry.client_order_id == f"traido-e-{intent.id.hex[:16]}"


# ── Test 11 · the same approval twice ────────────────────────────────────────


def test_11_approving_twice_places_one_order(desk) -> None:
    opp = desk.offer("AAPL")

    first = desk.approve(opp.id)
    second = desk.approve(opp.id)

    assert first.status_code == 200
    assert second.status_code == 200, "a repeated approval is answered, not errored"
    assert second.json()["status"] == "executed"

    assert len([m for m in desk.backend.placed if m.side == "buy"]) == 1
    assert len(_entry_intents()) == 1
    assert len(_open_positions()) == 1


# ── Test 12 · the venue accepted, the reply was lost ─────────────────────────


def test_12_a_lost_reply_does_not_produce_a_second_order(desk) -> None:
    """The single most expensive failure in order routing.

    The venue has the order. We do not know that. Retrying blind would double
    the position; giving up would leave shares unaccounted for. The correct
    behaviour is to look for the order we already sent, using the client id we
    chose before sending it.
    """
    desk.backend.drop_replies = 1
    opp = desk.offer("AAPL")

    first = desk.approve(opp.id)
    assert first.status_code == 409
    assert "UNKNOWN" in first.json()["detail"], first.text

    intent = _entry_intents()[0]
    assert intent.status is IntentStatus.UNKNOWN, "an unresolved submit is UNKNOWN, not failed"
    assert intent.client_order_id, "the handle for recovery must have been persisted first"

    submitted = [m for m in desk.backend.placed if m.side == "buy"]
    assert len(submitted) == 1, "the venue received exactly one order"


def test_12b_the_unresolved_symbol_is_then_blocked(desk) -> None:
    """Ambiguity blocks, rather than being retried into a duplicate."""
    desk.backend.drop_replies = 1
    first = desk.offer("AAPL")
    desk.approve(first.id)

    second = desk.offer("AAPL")
    response = desk.approve(second.id)

    assert response.status_code == 409
    assert "UNRESOLVED_BROKER_STATE" in response.json()["detail"]
    assert len([m for m in desk.backend.placed if m.side == "buy"]) == 1


# ── Tests 13 & 14 · partial fills ────────────────────────────────────────────


def test_13_a_partial_fill_becomes_a_position_of_the_filled_size(desk) -> None:
    """A partial fill is a position, not a failure."""
    desk.backend.fill_ratio = 0.5
    opp = desk.offer("AAPL")

    response = desk.approve(opp.id)
    assert response.status_code == 200, response.text

    entry = next(m for m in desk.backend.placed if m.side == "buy")
    ordered = Decimal(entry.qty)
    positions = _open_positions()
    assert len(positions) == 1
    held = Decimal(str(positions[0].qty))
    assert held == ordered / 2, f"ordered {ordered}, filled half, book says {held}"


def test_13b_protection_covers_the_filled_quantity_not_the_ordered_one(desk) -> None:
    """The dangerous direction is a stop larger than the position.

    It would sell shares we never received, which on a venue that permits it
    opens a short — in a system whose policy disables shorting.
    """
    desk.backend.fill_ratio = 0.5
    opp = desk.offer("AAPL")
    desk.approve(opp.id)

    entry = next(m for m in desk.backend.placed if m.side == "buy")
    stop = desk.backend.placed_of_type("stop")[0]
    assert Decimal(stop.qty) == Decimal(entry.qty) / 2


def test_14_the_unfilled_remainder_is_cancelled_and_the_fill_kept(desk) -> None:
    desk.backend.fill_ratio = 0.5
    opp = desk.offer("AAPL")
    desk.approve(opp.id)

    assert desk.backend.canceled, "the resting remainder must not be left working"
    positions = _open_positions()
    assert len(positions) == 1 and Decimal(str(positions[0].qty)) > 0, (
        "cancelling the remainder must not discard the shares already bought"
    )


# ── Test 15 · protection could not be placed ─────────────────────────────────


def test_15_a_failed_protective_stop_flattens_rather_than_leaving_it_naked(desk) -> None:
    desk.backend.reject_order_types = {"stop"}
    opp = desk.offer("AAPL")

    desk.approve(opp.id)

    stop_attempts = desk.backend.placed_of_type("stop")
    assert stop_attempts, "protection must at least have been attempted"

    emergency = [m for m in desk.backend.placed if m.side == "sell" and m.order_type == "market"]
    assert emergency, (
        f"an unprotectable position must be flattened, not left open: {desk.backend.mutations}"
    )
    assert _open_positions() == [], "the book must not carry a position the venue closed"


def test_15b_the_emergency_exit_is_durable(desk) -> None:
    """Recorded before it is sent, so a crash mid-flatten is recoverable."""
    desk.backend.reject_order_types = {"stop"}
    opp = desk.offer("AAPL")
    desk.approve(opp.id)

    emergency = _intents_with_prefix("emergency_exit:")
    assert len(emergency) == 1, f"expected one emergency intent, got {emergency}"
    assert emergency[0].side is OrderSide.SELL


# ── Test 16 · the venue's order book is unreadable ───────────────────────────


def test_16_an_unreadable_order_book_is_unverified_not_protected(desk) -> None:
    """ "We did not look" and "we looked and it is there" must never be the same state."""
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    assert _open_positions(), "the position under discussion has to exist first"

    desk.backend.open_orders_unreadable = True
    before = len(desk.backend.mutations)

    response = desk.refresh_broker()
    assert response.status_code == 200

    assert len(desk.backend.mutations) == before, (
        "a broker we cannot read is not a broker to place orders against: "
        f"{desk.backend.mutations[before:]}"
    )


def test_16b_the_unverified_state_is_audited(desk) -> None:
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    desk.backend.open_orders_unreadable = True
    desk.refresh_broker()

    events = _audit_event_types()
    assert "ProtectionUnverified" in events, (
        f"expected a named unverified event, saw {sorted(events)}"
    )


# ── Tests 17 & 18 · restart recovery ─────────────────────────────────────────


def test_17_a_restart_does_not_resubmit_an_entry_the_venue_already_has(desk) -> None:
    """Durability is the point of writing the intent before transmitting.

    The desk is restarted by discarding the in-process client and building a
    new one against the same database — which is what a process restart is,
    given that every store resolves its engine from configuration.
    """
    desk.backend.drop_replies = 1
    opp = desk.offer("AAPL")
    desk.approve(opp.id)

    submitted_before = len([m for m in desk.backend.placed if m.side == "buy"])
    assert submitted_before == 1

    intent = _entry_intents()[0]
    assert intent.status is IntentStatus.UNKNOWN

    from fastapi.testclient import TestClient

    from api.main import app

    restarted = TestClient(app)
    try:
        recovered = [i for i in _entry_intents() if i.id == intent.id]
        assert recovered and recovered[0].status is IntentStatus.UNKNOWN, (
            "the unresolved intent must survive the restart"
        )
        assert restarted.post(
            f"/api/v1/opportunities/{opp.id}/decide",
            json={
                "decision": "approve",
                "request_id": str(uuid4()),
                "expected_decision_version": opp.decision_version,
            },
        ).status_code in {409, 400, 422}
        assert len([m for m in desk.backend.placed if m.side == "buy"]) == submitted_before, (
            "a restart must not re-send an order the venue already holds"
        )
    finally:
        restarted.close()


def test_18_a_restart_during_an_exit_does_not_sell_twice(desk) -> None:
    """An exit whose reply was lost must not be re-sent by the process that comes back.

    Worse than the entry case it mirrors: a second entry buys stock the desk
    did not intend to own, but a second exit sells stock the desk does not own,
    and on a long-only book that is a short position nothing in the system is
    built to carry.
    """
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    position_id = desk.open_position_id("AAPL")

    desk.backend.drop_replies = 1
    exit_card = desk.offer_exit(position_id)
    desk.sell(exit_card.id)

    sells = [m for m in desk.backend.placed if m.side == "sell" and m.order_type != "stop"]
    assert len(sells) == 1, f"the exit was transmitted once, saw {len(sells)}"

    unresolved = _exit_intents()
    assert [i.status for i in unresolved] == [IntentStatus.UNKNOWN], (
        "a lost reply on an exit must leave one UNKNOWN intent, not a resolved one"
    )

    from fastapi.testclient import TestClient

    from api.main import app

    restarted = TestClient(app)
    try:
        survived = _exit_intents()
        assert [i.id for i in survived] == [unresolved[0].id], (
            "the unresolved exit intent must survive the restart"
        )
        assert survived[0].status is IntentStatus.UNKNOWN

        # Retried through a fresh card, because the first one is stuck mid-decision
        # and refusing on its status would prove nothing about idempotency. This
        # is the shape of the real retry: the position agent proposes the exit
        # again after the restart, and the operator presses sell again.
        again = restarted.post(
            f"/api/v1/exits/{desk.offer_exit(position_id).id}/decide",
            json={"decision": "sell"},
        )
        assert again.status_code == 200, again.text

        after = [m for m in desk.backend.placed if m.side == "sell" and m.order_type != "stop"]
        assert len(after) == 1, (
            f"a restart must not re-send an exit the venue already holds, saw {len(after)}"
        )

        resolved = _exit_intents()
        assert [i.id for i in resolved] == [unresolved[0].id], (
            "the retry must resume the original intent, not open a second one"
        )
        assert resolved[0].status is IntentStatus.FILLED, (
            "the retry resolves the unknown exit by reading the venue, "
            f"not by selling again — status was {resolved[0].status}"
        )
    finally:
        restarted.close()

    from trading.ledger import LEDGER

    assert LEDGER.get_open("AAPL") == [], "the position closed exactly once"
    assert desk.venue_holdings("AAPL") == 0.0
