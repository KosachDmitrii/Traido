"""Tests 19–24: broker truth, and what happens when two things run at once.

Everything up to here has been one request at a time against a venue that
agrees with the book. This file is the opposite case, and it is where the
findings in `docs/architecture/gap-register.md` become reproducible rather than
argued from code reading.

Concurrency is exercised with real threads against the real app. Two requests
that a browser could plausibly issue at the same moment — two dashboard tabs,
a prefetch, a retried poll — should not be able to double an order.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal


def _open_positions():
    from trading.ledger import LEDGER

    return LEDGER.get_open()


def _protective_stops(desk):
    return desk.backend.placed_of_type("stop")


def _in_parallel(calls) -> list:
    """Fire the given zero-argument calls at once and collect their results."""
    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        futures = [pool.submit(call) for call in calls]
        return [f.result() for f in futures]


def hold_both_readers_at_the_order_book(desk, monkeypatch, *, parties: int = 2) -> None:
    """Force two reconciliation passes to read the venue before either acts.

    Left to the scheduler, this race reproduces on some runs and not others,
    and a hazard that only sometimes appears is not evidence of anything. The
    interleaving is therefore pinned rather than hoped for: both passes take
    their snapshot of the open-order book, and only then is either allowed to
    continue. Nothing about the system under test is changed — this is the
    ordering the absence of a lock permits, made repeatable.
    """
    import asyncio

    original = desk.broker.list_open_orders
    state = {"arrived": 0, "gate": None}

    async def _paced() -> list:
        book = await original()
        if state["gate"] is None:
            state["gate"] = asyncio.Event()
        state["arrived"] += 1
        if state["arrived"] >= parties:
            state["gate"].set()
        elif state["arrived"] < parties:
            try:
                await asyncio.wait_for(state["gate"].wait(), timeout=5.0)
            except TimeoutError:  # the second pass never came; report honestly
                pass
        return book

    monkeypatch.setattr(desk.broker, "list_open_orders", _paced)


# ── Test 19 · the book says flat, the venue still holds shares ───────────────


def test_19_an_orphan_position_at_the_venue_is_not_silently_accepted(desk) -> None:
    """Broker truth wins, and disagreement is surfaced rather than smoothed over.

    The dangerous version of this bug is the quiet one: the desk shows a clean
    book, the venue holds shares nobody is watching, and no stop protects them.
    """
    desk.set_venue_holdings("AAPL", 40)
    assert _open_positions() == [], "the book starts flat — that is the premise"

    response = desk.refresh_broker()
    assert response.status_code == 200

    reported = response.json()["positions"]
    assert any(p["symbol"] == "AAPL" for p in reported), (
        "a position the venue holds must appear on the desk even when the book "
        f"has no row for it: {reported}"
    )


def test_19b_an_orphan_position_is_audited(desk) -> None:
    from tests.integration.test_execution_lifecycle_end_to_end import _audit_event_types

    desk.set_venue_holdings("AAPL", 40)
    desk.refresh_broker()

    events = _audit_event_types()
    assert any("Orphan" in e or "Discrepancy" in e or "Unknown" in e for e in events), (
        f"a venue position the book does not know about must be named, saw {sorted(events)}"
    )


# ── Test 20 · the book says open, the venue says zero ────────────────────────


def test_20_a_position_the_venue_no_longer_holds_is_reconciled(desk) -> None:
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    assert _open_positions(), "the position under discussion has to exist first"

    desk.set_venue_holdings("AAPL", None)
    desk.refresh_broker()

    remaining = _open_positions()
    assert remaining == [], (
        "the book must not keep carrying a position the venue says is gone: "
        f"{[(r.symbol, r.qty) for r in remaining]}"
    )


# ── Test 21 · partial exit ───────────────────────────────────────────────────


def test_21_a_partial_exit_reduces_the_book_by_the_fill_and_resizes_the_stop(desk) -> None:
    """Sell 100, get 50 away, and the other 50 are still a position.

    Two things have to move together and neither is optional. The book must come
    down by the filled quantity only — treating a partial sale as a close would
    leave fifty shares owned by an account the desk believes is flat. And the
    protective stop, which was cancelled to free the shares for the sale, has to
    come back at the *remaining* size: reinstated at the original quantity it
    would sell fifty shares that no longer exist.
    """
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    position_id = desk.open_position_id("AAPL")
    held_before = Decimal(str(_open_positions()[0].qty))

    desk.backend.fill_ratio = 0.5
    desk.sell(desk.offer_exit(position_id).id)

    still_open = _open_positions()
    assert len(still_open) == 1, "a half-filled exit is not a closed position"
    remaining = Decimal(str(still_open[0].qty))
    assert remaining == held_before / 2, f"held {held_before}, sold half, book says {remaining}"

    desk.assert_protection_never_exceeds_holdings("AAPL")
    resting = [
        Decimal(o["qty"])
        for o in desk.backend.orders.values()
        if o["side"] == "sell" and o["type"] == "stop" and o["status"] not in {"canceled", "filled"}
    ]
    assert resting, "the remainder must still be protected"
    assert sum(resting) == remaining, (
        f"stop must cover exactly the {remaining} shares left, covers {sum(resting)}"
    )


def test_21a_an_unexplained_shrink_blocks_the_symbol_rather_than_adjusting(desk) -> None:
    """Half the shares left and no exit of ours accounts for it.

    The right answer is not to quietly write 25 into the book. Something moved
    those shares that Traido did not do, and adopting the venue's number would
    erase the only evidence that anything is wrong. `reconcile_position_quantities`
    gets this right: it corrects a size it can explain from recorded exit fills,
    and blocks the symbol when it cannot.
    """
    from tests.integration.test_execution_lifecycle_end_to_end import _audit_event_types
    from trading import external_positions as ep

    opp = desk.offer("AAPL")
    desk.approve(opp.id)

    held_before = Decimal(str(_open_positions()[0].qty))
    assert held_before > 0

    desk.set_venue_holdings("AAPL", held_before / 2)
    desk.refresh_broker()

    assert "PositionQuantityMismatch" in _audit_event_types(), (
        "an unexplained disagreement with the venue must be named, not absorbed"
    )
    assert "AAPL" in ep.EXTERNAL_POSITIONS.blocking_symbols(), (
        "and the symbol must be blocked until a human resolves it"
    )
    assert Decimal(str(_open_positions()[0].qty)) == held_before, (
        "the book must not adopt a number it cannot explain"
    )


def test_21b_protection_is_never_larger_than_what_the_venue_holds(desk) -> None:
    """P0-6. Was red: protection was sized from the book, including when the
    book had just been proved wrong — a resting SELL for 50 above 25 held shares.

    Two changes make it green, and both are needed. Protection is now sized from
    the smaller of the book and the venue, and a sweep cancels any resting
    protective SELL beyond what the account holds however it got there.
    """
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    held_before = Decimal(str(_open_positions()[0].qty))

    desk.set_venue_holdings("AAPL", held_before / 2)
    desk.refresh_broker()

    desk.assert_protection_never_exceeds_holdings("AAPL")


# ── Test 22 · two approvals at once ──────────────────────────────────────────


def test_22_two_simultaneous_approvals_place_one_order(desk) -> None:
    """Within one process the claim is genuinely atomic — this pins that.

    Across processes it is not: `OpportunityStore.claim` serialises on a
    `threading.Lock`, so the guarantee this test establishes is a
    single-worker guarantee. See P1-6 in the gap register.
    """
    opp = desk.offer("AAPL")

    results = _in_parallel([lambda: desk.approve(opp.id), lambda: desk.approve(opp.id)])

    codes = sorted(r.status_code for r in results)
    assert codes in ([200, 200], [200, 400], [200, 409]), codes
    buys = [m for m in desk.backend.placed if m.side == "buy"]
    assert len(buys) == 1, f"two clicks, one order — got {len(buys)}: {buys}"
    assert len(_open_positions()) == 1
    # Claim is now DB compare-and-swap (`WHERE status` + FOR UPDATE), not only
    # a process lock. Cross-process still needs single-worker or shared DB CAS
    # under load; this test pins the in-process path that operators use today.


# ── Test 23 · two emergency triggers ─────────────────────────────────────────


def test_23_two_emergency_triggers_produce_one_close(desk, monkeypatch) -> None:
    """Emergency exits survive the race that duplicates protective stops.

    Two reconciliation passes overlap on the same unprotectable position, both
    decide to flatten, and only one market SELL leaves the process. The reason
    is the durable intent: the emergency key carries a generation counter, an
    unresolved intent for that position is reused rather than re-created, and
    the unique index on `idempotency_key` settles the tie in the database.

    Read this next to test 24, which is the same race against the same code path
    and does duplicate. The only structural difference between them is that a
    protective stop has no intent — which is the argument for P0-1's fix shape,
    made by the system's own behaviour rather than by assertion.
    """
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    desk.strand_position()
    desk.backend.reject_order_types = {"stop"}

    before = len(desk.backend.placed)
    hold_both_readers_at_the_order_book(desk, monkeypatch)
    _in_parallel([desk.refresh_broker, desk.refresh_broker])

    flattens = [
        m for m in desk.backend.placed[before:] if m.side == "sell" and m.order_type == "market"
    ]
    assert len(flattens) <= 1, f"one incident, one close — got {len(flattens)}"


# ── P1-3 · the sweep for protection nobody recorded ──────────────────────────


def test_a_stray_protective_sell_beyond_the_position_is_cancelled(desk) -> None:
    """The reverse question reconciliation never used to ask.

    The protection loop checks whether *the stop we recorded* is present and
    correctly sized. It cannot see a SELL the venue holds that the book knows
    nothing about — which is what every duplicate, every orphan and every
    oversized stop eventually looks like.
    """
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    held = Decimal(str(desk.venue_holdings("AAPL")))

    desk.plant_protective_sell("AAPL", held, client_order_id="stray-protective-sell")
    assert len(desk.resting_protection("AAPL")) == 2

    desk.refresh_broker()

    desk.assert_protection_never_exceeds_holdings("AAPL")


def test_the_excess_is_named_in_the_audit(desk) -> None:
    from tests.integration.test_execution_lifecycle_end_to_end import _audit_event_types

    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    desk.set_venue_holdings("AAPL", 1)

    desk.refresh_broker()

    assert "ExcessProtectionDetected" in _audit_event_types()
    desk.assert_protection_never_exceeds_holdings("AAPL")


# ── Test 24 · two reconciliation passes ──────────────────────────────────────


def test_24_two_reconciliation_passes_place_one_protective_stop(desk, monkeypatch) -> None:
    """Was red until P0-4; kept as the regression test for single-flight.

    Two passes read the same order book, both saw the same missing stop, and
    both replaced it. The supervisor now coalesces concurrent callers onto one
    pass, so the second never reads a book it is about to invalidate.

    This closes the *trigger*, not the underlying weakness: protective placement
    still has no durable intent, so it is not idempotent on its own. The test
    directly below is the one that holds that line.
    """
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    desk.strand_position()

    before = len(_protective_stops(desk))
    hold_both_readers_at_the_order_book(desk, monkeypatch)
    _in_parallel([desk.refresh_broker, desk.refresh_broker])

    replacements = len(_protective_stops(desk)) - before
    assert replacements == 1, f"one unprotected position, one replacement stop — got {replacements}"


def test_24a_a_lost_reply_on_a_protective_stop_does_not_place_a_second(desk) -> None:
    """P0-1 proper: idempotency that does not depend on a lock.

    Single-flight prevents two passes overlapping. It does nothing about the
    case where one pass sends a stop, the venue accepts it and the reply is
    lost — the same ambiguity the entry path handles with a durable intent and a
    derived client id. Without one, the next pass sees no recorded stop, cannot
    recognise the order it already sent, and sends another.

    The venue's own view is what is asserted, not the tape: what matters is how
    many resting SELLs sit above the position, however many requests it took.
    """
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    desk.strand_position()

    desk.backend.drop_replies = 1
    desk.refresh_broker()
    desk.refresh_broker()

    desk.assert_protection_never_exceeds_holdings("AAPL")


def test_24b_a_single_reconciliation_pass_restores_protection_exactly_once(desk) -> None:
    """The same operation without the race, so the fix has a green baseline.

    Running it twice in sequence must also be a no-op the second time: repeated
    reconciliation is a control loop, and a control loop that acts on every pass
    is an oscillator.
    """
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    desk.strand_position()

    before = len(_protective_stops(desk))
    desk.refresh_broker()
    after_first = len(_protective_stops(desk))
    desk.refresh_broker()
    after_second = len(_protective_stops(desk))

    assert after_first - before == 1, "the missing stop must be replaced"
    assert after_second == after_first, (
        "the second pass must see the stop it just placed and do nothing"
    )
