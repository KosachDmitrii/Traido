"""A halted desk must still be able to defend the positions it already holds.

The kill switch exists to stop Traido taking on risk. It was implemented as a
refusal at `AlpacaBroker.place_order`, which is the one layer that cannot tell
an entry from a protective stop — so halting the desk also disarmed it. With the
switch on, reconciliation would find an unprotected position, ask for a stop, and
be refused; an emergency flatten would be refused the same way.

That inverts the switch's purpose. It is pressed precisely when something has
gone wrong, which is exactly when open positions most need a stop and an exit.
The policy this file pins is the one stated in `AGENTS.md` and in the failure
matrix: new exposure is refused, risk reduction is not.
"""

from __future__ import annotations

from decimal import Decimal

import pytest


def _open_positions():
    from trading.ledger import LEDGER

    return LEDGER.get_open()


@pytest.fixture
def halted():
    """Halt the desk, and lift it again whatever the test does."""
    from risk.kill_switch import set_kill_switch

    def _halt() -> None:
        set_kill_switch(True, actor="test", reason="integration")

    yield _halt
    set_kill_switch(False, actor="test", reason="integration teardown")


def test_the_kill_switch_still_refuses_a_new_entry(desk, halted) -> None:
    """The half that must keep working, asserted alongside the half that must not.

    A fix that let risk reduction through by weakening the entry refusal would
    be worse than the bug.
    """
    opp = desk.offer("AAPL")
    halted()

    response = desk.approve(opp.id)

    assert response.status_code != 200, "a halted desk must not open a position"
    assert not [m for m in desk.backend.placed if m.side == "buy"], (
        "nothing may reach the venue while the desk is halted"
    )


def test_a_halted_desk_can_still_protect_an_unprotected_position(desk, halted) -> None:
    """Reconciliation finds a stranded position while the desk is halted.

    Protection is external state that a venue can drop at any time, including
    during the incident that caused the halt. If the switch blocks the repair,
    the shares sit unprotected for as long as the desk stays halted — which is
    the opposite of what pressing it was meant to achieve.
    """
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    held = Decimal(str(_open_positions()[0].qty))
    assert held > 0

    desk.strand_position()
    halted()
    desk.refresh_broker()

    resting = [
        Decimal(o["qty"])
        for o in desk.backend.orders.values()
        if o["side"] == "sell" and o["type"] == "stop" and o["status"] not in {"canceled", "filled"}
    ]
    assert resting, "a halted desk must still re-arm the stop on an open position"
    assert sum(resting) == held, f"stop must cover the {held} shares held, covers {sum(resting)}"


def test_a_halted_desk_can_still_flatten_a_position_it_cannot_protect(desk, halted) -> None:
    """The backstop has to work when the desk is halted, or it is not a backstop.

    The position is opened normally, and only then does everything go wrong at
    once — which is the realistic order of events, since the halt is usually a
    response to the trouble rather than something that precedes it. The venue
    has dropped the stop and now refuses to accept a new one, so the only way to
    stop the bleeding is to close. If the kill switch blocks that, the incident
    ends with shares held, no protection, and no way out.
    """
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    assert _open_positions(), "the position must exist before the desk is halted"

    desk.strand_position()
    desk.backend.reject_order_types = {"stop"}
    halted()
    desk.refresh_broker()

    flattens = [m for m in desk.backend.placed if m.side == "sell" and m.order_type == "market"]
    assert flattens, "an unprotectable position must be closed even while halted"
    assert desk.venue_holdings("AAPL") == 0.0, "and the shares must actually be gone"


def test_an_operator_can_still_close_a_position_by_hand_while_halted(desk, halted) -> None:
    """Pressing the switch must not take away the operator's way out.

    Halting the desk is a decision to stop trading, made by someone who is
    usually about to go and look at the book. If the sell button stops working
    at that moment, the only remaining routes to flat are the emergency path
    and the broker's own terminal — so the safe action becomes the awkward one.
    """
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    position_id = desk.open_position_id("AAPL")

    halted()
    response = desk.sell(desk.offer_exit(position_id).id)

    assert response.status_code == 200, f"a manual close must survive the halt: {response.text}"
    assert desk.venue_holdings("AAPL") == 0.0
    assert _open_positions() == [], "and the book must record it closed"
