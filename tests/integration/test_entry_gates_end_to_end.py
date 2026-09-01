"""Tests 1–9: every mandatory entry gate, refusing from the route inwards.

Each test does the same thing. Put a real card on the desk, break exactly one
precondition, `POST /api/v1/opportunities/{id}/decide` with `approve`, and
assert two things: the named reason came back, and **nothing reached the
broker**. The second assertion is the one that matters. A gate that rejects but
has already sent an order is not a gate, and only the transport tape can tell
the difference.

Every unit test covering these gates builds `ExecutionService` by hand. That
proves the gate. It does not prove the route reaches it, which is the property
that was false for the liquidity gate for three stages.
"""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from core.enums import EarningsCheck

ET = ZoneInfo("America/New_York")


def _detail(response) -> str:
    assert response.status_code == 409, (
        f"expected the desk to refuse, got {response.status_code}: {response.text}"
    )
    return response.json()["detail"]


# ── Test 1 · market data missing ─────────────────────────────────────────────


def test_1_no_market_data_port_refuses_the_entry(desk, monkeypatch) -> None:
    """The historical defect, reproduced from the route.

    `build_execution_service` supplies the port today, so this reaches in and
    takes it away again. That is not a contrived scenario: it is precisely the
    state the desk shipped in, and the only thing that changed is that the
    service now refuses instead of shrugging.
    """
    from api import deps

    monkeypatch.setattr(deps, "create_market_data_port", lambda _s: None)
    opp = desk.offer("AAPL")

    detail = _detail(desk.approve(opp.id))

    assert "MARKET_DATA_NOT_CONFIGURED" in detail
    assert "LIQUIDITY_GATE_REJECTED" in detail
    desk.assert_no_broker_mutations()


# ── Test 2 · no live quote ───────────────────────────────────────────────────


def test_2_a_missing_quote_refuses_the_entry(desk) -> None:
    """An unmeasured spread is not a narrow one."""
    desk.market.quote_available = False
    opp = desk.offer("AAPL")

    detail = _detail(desk.approve(opp.id))

    assert "LIVE_QUOTE_REQUIRED" in detail
    desk.assert_no_broker_mutations()


# ── Test 3 · stale quote ─────────────────────────────────────────────────────


def test_3_a_stale_quote_refuses_the_entry(desk) -> None:
    """Distinguished from a missing one on purpose.

    `LIVE_QUOTE_REQUIRED` says the venue never answered; `QUOTE_STALE` says it
    answered about a market that has since moved on. Collapsing them would hide
    a feed that is up and lagging, which is the more dangerous of the two
    because everything looks healthy.
    """
    desk.market.quote_age_sec = 120.0
    opp = desk.offer("AAPL")

    detail = _detail(desk.approve(opp.id))

    assert "QUOTE_STALE" in detail
    desk.assert_no_broker_mutations()


# ── Test 4 · spread too wide ─────────────────────────────────────────────────


def test_4_a_wide_spread_refuses_the_entry(desk) -> None:
    desk.market.spread_bps = 400.0
    opp = desk.offer("AAPL")

    detail = _detail(desk.approve(opp.id))

    assert "SPREAD_TOO_WIDE" in detail
    assert "LIQUIDITY_GATE_REJECTED" in detail
    desk.assert_no_broker_mutations()


def test_4b_an_illiquid_symbol_refuses_the_entry(desk) -> None:
    """The other half of the gate: thin tape rather than a wide quote."""
    desk.market.volume = 1_000.0
    opp = desk.offer("AAPL")

    detail = _detail(desk.approve(opp.id))

    assert "INSUFFICIENT_AVG_DOLLAR_VOLUME" in detail
    desk.assert_no_broker_mutations()


def test_4c_a_market_data_outage_refuses_the_entry(desk) -> None:
    """Distinct from a missing port: this one clears on its own, config does not."""
    desk.market.raise_on_bars = True
    opp = desk.offer("AAPL")

    detail = _detail(desk.approve(opp.id))

    assert "MARKET_DATA_UNAVAILABLE" in detail
    desk.assert_no_broker_mutations()


# ── Test 5 · outside regular hours ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("when", "reason"),
    [
        ((2026, 3, 10, 18, 30), "AFTER_HOURS"),
        ((2026, 3, 10, 7, 0), "PREMARKET"),
        ((2026, 3, 14, 11, 0), "MARKET_CLOSED_WEEKEND"),
    ],
)
def test_5_outside_rth_refuses_the_entry(desk, monkeypatch, when, reason) -> None:
    """New exposure is RTH-only. Protective exits deliberately are not — see test 5b."""
    from datetime import datetime

    from trading import execution

    monkeypatch.setattr(execution, "_utcnow", lambda: datetime(*when, tzinfo=ET))
    opp = desk.offer("AAPL")

    detail = _detail(desk.approve(opp.id))

    assert reason in detail
    assert "RTH_GATE_REJECTED" in detail
    desk.assert_no_broker_mutations()


# ── Test 6 · event risk ──────────────────────────────────────────────────────


def _calendar(monkeypatch, *, status: EarningsCheck, next_date: date | None = None) -> None:
    from market_data.providers import earnings as earnings_mod

    class _Scripted:
        configured = status is not EarningsCheck.NOT_CONFIGURED

        async def get(self, symbol: str, *, now=None):
            return earnings_mod.EarningsInfo(
                symbol=symbol.upper(),
                status=status,
                next_date=next_date,
                note="scripted calendar",
            )

    monkeypatch.setattr("risk.context_builder.get_earnings_calendar", lambda _k: _Scripted())


def _exchange_today() -> date:
    """The exchange day the execution clock is frozen to.

    Not `market_date()` with no argument: that reads the wall clock, and the
    suite pins execution to a Tuesday in March. Counting the blackout window
    from the real date would put the print six months out and the gate would
    correctly let the trade through, for a reason that has nothing to do with
    what the test is asserting.
    """
    from core.clock import market_date
    from trading import execution

    return market_date(execution._utcnow())


def test_6_an_imminent_print_refuses_the_entry(desk, monkeypatch) -> None:
    _calendar(
        monkeypatch, status=EarningsCheck.CHECKED, next_date=_exchange_today() + timedelta(days=1)
    )
    opp = desk.offer("AAPL")

    detail = _detail(desk.approve(opp.id))

    assert "EARNINGS_IMMINENT" in detail
    assert "RISK_REJECT" in detail
    desk.assert_no_broker_mutations()


def test_6b_an_unreadable_calendar_refuses_the_entry(desk, monkeypatch) -> None:
    """An unread check is not a passed check.

    The card is seeded while the calendar still reads clear, then the calendar
    goes dark before the click. That ordering is the point: approval re-derives
    the risk context, so the gap has to be caught at the moment capital moves,
    not only at the moment the card was drawn.
    """
    opp = desk.offer("AAPL")
    _calendar(monkeypatch, status=EarningsCheck.UNAVAILABLE)

    detail = _detail(desk.approve(opp.id))

    assert "EARNINGS_CALENDAR_UNAVAILABLE" in detail
    desk.assert_no_broker_mutations()


def test_6c_an_unconfigured_calendar_refuses_the_entry(desk, monkeypatch) -> None:
    opp = desk.offer("AAPL")
    _calendar(monkeypatch, status=EarningsCheck.NOT_CONFIGURED)

    detail = _detail(desk.approve(opp.id))

    assert "EARNINGS_CALENDAR_NOT_CONFIGURED" in detail
    desk.assert_no_broker_mutations()


# ── Test 7 · broker link not READY ───────────────────────────────────────────


@pytest.mark.parametrize("state", ["DISCONNECTED", "CONNECTING", "DEGRADED", "RECONNECTING"])
def test_7_a_broker_that_is_not_ready_refuses_the_entry(desk, state: str) -> None:
    from core.enums import BrokerConnectionState

    desk.broker.link_state = BrokerConnectionState[state]
    opp = desk.offer("AAPL")

    detail = _detail(desk.approve(opp.id))

    assert f"BROKER_{state}" in detail
    assert "CONNECTIVITY_GATE_REJECTED" in detail
    desk.assert_no_broker_mutations()


# ── Test 8 · stale reconciliation ────────────────────────────────────────────


def test_8_stale_reconciliation_refuses_the_entry(desk) -> None:
    """P0-5. Reconciliation age was computed and rendered; no gate read it.

    Every other entry gate reasons about the market. This one reasons about us:
    position count, open exposure, whether this symbol is already held — the
    risk engine judges all of it against a local book, and reconciliation is the
    only thing keeping that book honest. Once it stops, the numbers do not
    become obviously wrong, they become confidently wrong.
    """
    opp = desk.offer("AAPL")
    desk.age_reconciliation(3600)

    detail = _detail(desk.approve(opp.id))

    assert "RECONCILIATION_STALE" in detail
    desk.assert_no_broker_mutations()


def test_8b_a_desk_that_has_never_reconciled_refuses_the_entry(desk) -> None:
    """Cold start is a distinct state and gets a distinct reason.

    "Never checked" and "checked an hour ago" both block the trade but call for
    different operator responses, so the gate does not collapse them into one
    message. This is also the state every process is in for its first seconds,
    which is why the fix could not land before the background loop existed.
    """
    from trading.reconcile_supervisor import RECONCILE

    opp = desk.offer("AAPL")
    RECONCILE.reset()

    detail = _detail(desk.approve(opp.id))

    assert "RECONCILIATION_NEVER_RAN" in detail
    desk.assert_no_broker_mutations()


def test_8c_a_fresh_pass_re_arms_the_desk(desk) -> None:
    """The gate has to clear on its own, or the response to it is to remove it."""
    opp = desk.offer("AAPL")
    desk.age_reconciliation(3600)
    assert "RECONCILIATION_STALE" in _detail(desk.approve(opp.id))

    desk.reconcile_now()
    retry = desk.approve(opp.id)

    assert retry.status_code == 200, retry.text


def test_8d_protective_work_is_not_gated_on_freshness(desk) -> None:
    """Refusing to defend an open position because truth is stale is backwards.

    When broker truth is doubtful the correct response is to read more and take
    on less — not to leave a naked long naked because the last pass was late.
    """
    opp = desk.offer("AAPL")
    desk.approve(opp.id)
    desk.strand_position()
    desk.age_reconciliation(3600)

    desk.refresh_broker()

    assert len(desk.resting_protection("AAPL")) == 1, "the stop must still be restored"


# ── P1-5 · data freshness ────────────────────────────────────────────────────


def test_bars_a_week_old_refuse_the_entry(desk) -> None:
    """The quote had a fifteen-second age limit; the bars behind it had none.

    Average dollar volume, the price floor and expected slippage all come out of
    the daily series, so a feed that stopped last week does not produce a
    cautious verdict — it produces a confident one computed from a market that
    has since moved.
    """
    desk.market.bar_age_days = 8
    opp = desk.offer("AAPL")

    detail = _detail(desk.approve(opp.id))

    assert "STALE_BARS" in detail
    desk.assert_no_broker_mutations()


def test_a_weekend_gap_does_not_refuse_the_entry(desk) -> None:
    """A threshold that fires after a long weekend gets switched off within the week."""
    desk.market.bar_age_days = 3
    opp = desk.offer("AAPL")

    assert desk.approve(opp.id).status_code == 200


# ── P1-4 · instrument eligibility ────────────────────────────────────────────


def test_an_otc_shaped_symbol_is_refused_on_the_alpaca_path(desk) -> None:
    """A name outside the sector map never reaches the book.

    Before the sector check, the Alpaca adapter reported no security type and
    `allowed_symbols` defaulted to `None`, so only the liquidity/instrument gate
    stood between the scanner and an unlisted name. An unclassified sector is
    now refused first — the same fail-closed posture — and `SYMBOL_LOOKS_OTC`
    remains the backstop when a name is somehow classified yet still OTC-shaped.
    """
    opp = desk.offer("ABCDF")

    detail = _detail(desk.approve(opp.id))

    assert (
        "SECTOR_UNCLASSIFIED" in detail
        or "SECTOR_NOT_CONFIGURED" in detail
        or "SYMBOL_LOOKS_OTC" in detail
    )
    desk.assert_no_broker_mutations()


def test_an_ordinary_listed_ticker_still_passes(desk) -> None:
    opp = desk.offer("AAPL")

    assert desk.approve(opp.id).status_code == 200


# ── Test 9 · unresolved broker state ─────────────────────────────────────────


def test_9_an_unknown_intent_blocks_a_conflicting_entry(desk) -> None:
    """`UNKNOWN` means broker truth is unresolved, and it blocks that symbol.

    The intent is written through the same store the execution service uses, so
    this is the state a lost submit reply actually leaves behind — not a mock
    standing in for one.
    """
    from decimal import Decimal
    from uuid import uuid4

    from core.enums import IntentStatus, OrderSide, OrderType
    from trading.intents import INTENTS
    from trading.order_intent import OrderIntent

    INTENTS.create_or_get(
        OrderIntent(
            idempotency_key=f"entry:{uuid4()}:0",
            broker="AlpacaPaperBroker",
            broker_account_id=None,
            symbol="AAPL",
            side=OrderSide.BUY,
            requested_qty=Decimal(10),
            order_type=OrderType.LIMIT,
            limit_price=Decimal(100),
            status=IntentStatus.UNKNOWN,
        )
    )
    opp = desk.offer("AAPL")

    detail = _detail(desk.approve(opp.id))

    assert "UNRESOLVED_BROKER_STATE" in detail
    desk.assert_no_broker_mutations()


def test_9b_an_unknown_intent_does_not_block_a_different_symbol(desk) -> None:
    """The block is per symbol. Blocking the whole desk would be its own hazard."""
    from decimal import Decimal
    from uuid import uuid4

    from core.enums import IntentStatus, OrderSide, OrderType
    from trading.intents import INTENTS
    from trading.order_intent import OrderIntent

    INTENTS.create_or_get(
        OrderIntent(
            idempotency_key=f"entry:{uuid4()}:0",
            broker="AlpacaPaperBroker",
            broker_account_id=None,
            symbol="TSLA",
            side=OrderSide.BUY,
            requested_qty=Decimal(10),
            order_type=OrderType.LIMIT,
            limit_price=Decimal(100),
            status=IntentStatus.UNKNOWN,
        )
    )
    opp = desk.offer("AAPL")

    assert desk.approve(opp.id).status_code == 200
    assert [m.symbol for m in desk.backend.placed] == ["AAPL", "AAPL"], (
        "expected the entry and its protective stop"
    )


# ── The kill switch, which is not numbered but gates the same path ───────────


def test_the_kill_switch_refuses_the_entry(desk) -> None:
    opp = desk.offer("AAPL")
    assert desk.client.post("/api/v1/kill-switch", json={"enabled": True}).status_code == 200

    detail = _detail(desk.approve(opp.id))

    assert "KILL_SWITCH" in detail
    desk.assert_no_broker_mutations()
