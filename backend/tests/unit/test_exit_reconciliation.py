"""
Exit reconciliation.

The book and the broker can disagree in a handful of specific ways, and each
one has exactly one honest response. Where a discrepancy is explainable, the
book moves. Where it is not, the symbol is blocked. Nothing is assumed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from core.audit import InMemoryAudit
from core.enums import (
    IntentPurpose,
    IntentStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    TradeAction,
    TradingMode,
)
from core.schemas import OrderRecord, PortfolioSnapshot, Position, TradeCandidate
from tests.support import CLEARED_EARNINGS
from trading.intents import MemoryOrderIntentStore
from trading.ledger import PositionLedger
from trading.opportunities import MemoryOpportunityStore
from trading.order_intent import OrderIntent, exit_idempotency_key
from trading.reconcile import (
    ReconciliationReport,
    reconcile_order_intents,
    reconcile_position_quantities,
    reconcile_protective_orders,
)

pytestmark = pytest.mark.asyncio


class _Broker:
    environment = "paper"

    def __init__(
        self,
        *,
        positions: dict[str, Decimal] | None = None,
        orders: list[OrderRecord] | None = None,
        open_orders: list[OrderRecord] | None = None,
    ) -> None:
        self._positions = positions or {}
        self._orders = {o.broker_order_id: o for o in (orders or []) if o.broker_order_id}
        self._by_client = {o.client_order_id: o for o in (orders or [])}
        self._open = open_orders or []

    async def get_portfolio(self) -> PortfolioSnapshot:
        raise NotImplementedError

    async def list_positions(self) -> list[Position]:
        return [
            Position(
                id=uuid4(),
                symbol=sym,
                qty=qty,
                avg_entry=Decimal(100),
                status=PositionStatus.OPEN,
                opened_at=datetime.now(UTC),
            )
            for sym, qty in self._positions.items()
        ]

    async def list_open_orders(self) -> list[OrderRecord]:
        return list(self._open)

    async def place_order(self, request: object) -> OrderRecord:
        raise AssertionError("reconciliation must never place orders itself")

    async def cancel_order(self, broker_order_id: str) -> OrderRecord:
        return self._orders[broker_order_id]

    async def get_order(self, broker_order_id: str) -> OrderRecord:
        found = self._orders.get(broker_order_id)
        if found is None:
            raise RuntimeError(f"unknown order {broker_order_id}")
        return found

    async def find_order_by_client_id(self, client_order_id: str) -> OrderRecord | None:
        return self._by_client.get(client_order_id)


def _sell(
    *,
    oid: str,
    status: OrderStatus,
    qty: Decimal,
    filled: Decimal | None,
    order_type: OrderType = OrderType.MARKET,
) -> OrderRecord:
    return OrderRecord(
        id=uuid4(),
        client_order_id=f"traido-x-{oid}",
        broker_order_id=oid,
        symbol="AAPL",
        side=OrderSide.SELL,
        order_type=order_type,
        qty=qty,
        status=status,
        filled_qty=filled,
        filled_avg_price=Decimal(110) if filled else None,
        stop_price=Decimal(95) if order_type is OrderType.STOP else None,
    )


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> PositionLedger:
    from sqlalchemy import create_engine

    import trading.intents
    import trading.ledger
    import trading.reconcile

    isolated = PositionLedger(create_engine("sqlite://", future=True))
    monkeypatch.setattr(trading.ledger, "LEDGER", isolated)
    monkeypatch.setattr(trading.reconcile, "LEDGER", isolated)
    monkeypatch.setattr(trading.intents, "LEDGER", isolated, raising=False)
    return isolated


def _position(ledger: PositionLedger, *, qty: Decimal, stop_order_id: str | None = None) -> UUID:
    from risk.risk_engine import RiskEngine

    candidate = TradeCandidate(
        symbol="AAPL",
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal(100),
        stop=Decimal(95),
        target=Decimal(120),
        risk_reward=4.0,
        reasons=["fixture"],
        strategy_version="test-v1",
    )
    snapshot = PortfolioSnapshot(
        equity=Decimal(100_000),
        cash=Decimal(100_000),
        buying_power=Decimal(100_000),
        open_exposure=Decimal(0),
        open_positions=0,
        day_pnl=Decimal(0),
        week_pnl=Decimal(0),
        drawdown_pct=0.0,
        kill_switch=False,
    )
    opp = MemoryOpportunityStore().create(
        candidate,
        RiskEngine().evaluate(candidate, snapshot, context=CLEARED_EARNINGS),
        TradingMode.CONFIRMATION,
    )
    return ledger.open_from_opportunity(
        opp,
        qty=qty,
        broker_entry_order_id="entry-1",
        fill_price=Decimal(100),
        stop_order_id=stop_order_id,
    ).id


def _exit_intent(
    intents: MemoryOrderIntentStore,
    position_id: UUID,
    *,
    status: IntentStatus,
    broker_order_id: str,
    qty: Decimal = Decimal(100),
    purpose: IntentPurpose = IntentPurpose.EXIT,
    applied_exit_qty: Decimal = Decimal(0),
) -> OrderIntent:
    intent, _ = intents.create_or_get(
        OrderIntent(
            idempotency_key=exit_idempotency_key(position_id, 0),
            purpose=purpose,
            broker="test",
            symbol="AAPL",
            side=OrderSide.SELL,
            requested_qty=qty,
            order_type=OrderType.MARKET,
            position_id=position_id,
            status=status,
            broker_order_id=broker_order_id,
            client_order_id=f"traido-x-{broker_order_id}",
            applied_exit_qty=applied_exit_qty,
        )
    )
    return intent


# ── A: local SUBMITTED, broker FILLED ────────────────────────────────────────


async def test_an_exit_that_filled_while_we_were_away_closes_the_position(
    ledger: PositionLedger,
) -> None:
    position_id = _position(ledger, qty=Decimal(100))
    intents = MemoryOrderIntentStore()
    intent = _exit_intent(
        intents, position_id, status=IntentStatus.SUBMITTED, broker_order_id="x-1"
    )
    broker = _Broker(
        orders=[_sell(oid="x-1", status=OrderStatus.FILLED, qty=Decimal(100), filled=Decimal(100))]
    )
    audit = InMemoryAudit()

    await reconcile_order_intents(broker, intents, audit)  # type: ignore[arg-type]

    assert intents.get(intent.id).status is IntentStatus.FILLED
    assert ledger.get_open("AAPL") == []
    assert any(e["event_type"] == "ExitFilled" for e in audit.events)


# ── B: local PARTIALLY_FILLED, broker CANCELLED ──────────────────────────────


async def test_a_cancelled_exit_with_a_partial_fill_keeps_the_remainder_open(
    ledger: PositionLedger,
) -> None:
    position_id = _position(ledger, qty=Decimal(100))
    intents = MemoryOrderIntentStore()
    _exit_intent(intents, position_id, status=IntentStatus.SUBMITTED, broker_order_id="x-2")
    broker = _Broker(
        orders=[_sell(oid="x-2", status=OrderStatus.CANCELED, qty=Decimal(100), filled=Decimal(40))]
    )

    await reconcile_order_intents(broker, intents, InMemoryAudit())  # type: ignore[arg-type]

    row = ledger.get(position_id)
    assert row is not None and row.status == "open"
    assert Decimal(str(row.qty)) == Decimal(60)


# ── C: local UNKNOWN, broker FILLED ──────────────────────────────────────────


async def test_an_unknown_exit_resolves_when_the_broker_can_answer(
    ledger: PositionLedger,
) -> None:
    position_id = _position(ledger, qty=Decimal(100))
    intents = MemoryOrderIntentStore()
    intent = _exit_intent(intents, position_id, status=IntentStatus.UNKNOWN, broker_order_id="x-3")
    broker = _Broker(
        orders=[_sell(oid="x-3", status=OrderStatus.FILLED, qty=Decimal(100), filled=Decimal(100))]
    )

    await reconcile_order_intents(broker, intents, InMemoryAudit())  # type: ignore[arg-type]

    assert intents.get(intent.id).status is IntentStatus.FILLED
    assert ledger.get_open("AAPL") == []


# ── D: local UNKNOWN, broker has no trace ────────────────────────────────────


async def test_an_exit_the_broker_cannot_account_for_stays_unknown(
    ledger: PositionLedger,
) -> None:
    position_id = _position(ledger, qty=Decimal(100))
    intents = MemoryOrderIntentStore()
    intent = _exit_intent(
        intents, position_id, status=IntentStatus.SUBMITTED, broker_order_id="ghost"
    )
    audit = InMemoryAudit()

    report = await reconcile_order_intents(_Broker(), intents, audit)  # type: ignore[arg-type]

    assert intents.get(intent.id).status is IntentStatus.UNKNOWN
    assert report.severity == "critical"
    assert Decimal(str(ledger.get(position_id).qty)) == Decimal(100), "no guessing"
    assert any(e["event_type"] == "ExitStateUnknown" for e in audit.events)


# ── Repeatability ────────────────────────────────────────────────────────────


async def test_running_reconciliation_twice_does_not_sell_the_position_twice(
    ledger: PositionLedger,
) -> None:
    """The bug this guards: each pass re-reads the same filled exit."""
    position_id = _position(ledger, qty=Decimal(100))
    intents = MemoryOrderIntentStore()
    _exit_intent(intents, position_id, status=IntentStatus.SUBMITTED, broker_order_id="x-4")
    broker = _Broker(
        orders=[_sell(oid="x-4", status=OrderStatus.CANCELED, qty=Decimal(100), filled=Decimal(40))]
    )

    await reconcile_order_intents(broker, intents, InMemoryAudit())  # type: ignore[arg-type]
    await reconcile_order_intents(broker, intents, InMemoryAudit())  # type: ignore[arg-type]
    await reconcile_order_intents(broker, intents, InMemoryAudit())  # type: ignore[arg-type]

    assert Decimal(str(ledger.get(position_id).qty)) == Decimal(60)


async def test_a_fill_the_book_already_absorbed_is_not_absorbed_again(
    ledger: PositionLedger,
) -> None:
    """A crash between the ledger write and the intent update.

    The live exit path reduces the position first and records the intent's new
    status second, so a process that dies in between leaves a durable record
    that looks unfinished over a book that is already correct. Reconciliation
    then reads the very same 40 filled shares off the broker. What stops it
    selling them a second time on paper is the absorbed-quantity record, not the
    intent's status — the status is exactly what did not get written.
    """
    position_id = _position(ledger, qty=Decimal(100))
    ledger.set_quantity(position_id, Decimal(60))  # the book already took the 40

    intents = MemoryOrderIntentStore()
    intent = _exit_intent(
        intents,
        position_id,
        status=IntentStatus.SUBMITTED,
        broker_order_id="x-9",
        qty=Decimal(40),
        applied_exit_qty=Decimal(40),
    )
    broker = _Broker(
        orders=[_sell(oid="x-9", status=OrderStatus.FILLED, qty=Decimal(40), filled=Decimal(40))]
    )

    for _ in range(3):
        await reconcile_order_intents(broker, intents, InMemoryAudit())  # type: ignore[arg-type]

    row = ledger.get(position_id)
    assert row is not None and row.status == "open", "60 shares are still ours"
    assert Decimal(str(row.qty)) == Decimal(60), "the same fill must not reduce the book twice"
    assert intents.get(intent.id).status is IntentStatus.FILLED


async def test_an_exit_that_keeps_filling_is_absorbed_as_it_goes(
    ledger: PositionLedger,
) -> None:
    """A resting exit fills 40, then 70, and never changes status doing it.

    PARTIALLY_FILLED at 40 and PARTIALLY_FILLED at 70 are the same status and
    two different positions, so a reconciliation that only reacts to status
    changes would leave the book claiming shares that have already been sold.
    Each pass absorbs the difference and nothing more.
    """
    position_id = _position(ledger, qty=Decimal(100))
    intents = MemoryOrderIntentStore()
    intent = _exit_intent(
        intents, position_id, status=IntentStatus.SUBMITTED, broker_order_id="x-10"
    )

    async def pass_with(filled: Decimal) -> None:
        broker = _Broker(
            orders=[_sell(oid="x-10", status=OrderStatus.PARTIAL, qty=Decimal(100), filled=filled)]
        )
        await reconcile_order_intents(broker, intents, InMemoryAudit())  # type: ignore[arg-type]

    await pass_with(Decimal(40))
    assert Decimal(str(ledger.get(position_id).qty)) == Decimal(60)

    await pass_with(Decimal(40))  # nothing new happened at the broker
    assert Decimal(str(ledger.get(position_id).qty)) == Decimal(60), "no double absorption"

    await pass_with(Decimal(70))  # 30 more shares left
    assert Decimal(str(ledger.get(position_id).qty)) == Decimal(30)
    assert intents.get(intent.id).applied_exit_qty == Decimal(70)


# ── E: quantity disagreement ─────────────────────────────────────────────────


async def test_a_size_gap_explained_by_known_exit_fills_is_corrected(
    ledger: PositionLedger,
) -> None:
    position_id = _position(ledger, qty=Decimal(100))
    intents = MemoryOrderIntentStore()
    intent = _exit_intent(
        intents, position_id, status=IntentStatus.PARTIALLY_FILLED, broker_order_id="x-5"
    )
    intents.update_fields(intent.id, filled_qty=Decimal(60))
    audit = InMemoryAudit()

    adjusted = await reconcile_position_quantities(
        ledger,
        intents,
        {
            "AAPL": Position(
                id=uuid4(),
                symbol="AAPL",
                qty=Decimal(40),
                avg_entry=Decimal(100),
                status=PositionStatus.OPEN,
                opened_at=datetime.now(UTC),
            )
        },
        audit,
    )

    assert adjusted == 1
    assert Decimal(str(ledger.get(position_id).qty)) == Decimal(40)
    assert any(e["event_type"] == "PositionQuantityReconciled" for e in audit.events)


async def test_an_unexplained_size_gap_blocks_the_symbol(ledger: PositionLedger) -> None:
    position_id = _position(ledger, qty=Decimal(100))
    intents = MemoryOrderIntentStore()
    audit = InMemoryAudit()
    report = ReconciliationReport()

    await reconcile_position_quantities(
        ledger,
        intents,
        {
            "AAPL": Position(
                id=uuid4(),
                symbol="AAPL",
                qty=Decimal(40),
                avg_entry=Decimal(100),
                status=PositionStatus.OPEN,
                opened_at=datetime.now(UTC),
            )
        },
        audit,
        report=report,
    )

    assert Decimal(str(ledger.get(position_id).qty)) == Decimal(100), "never invent a number"
    assert report.severity == "critical"
    from trading import external_positions as ep

    assert "AAPL" in ep.EXTERNAL_POSITIONS.blocking_symbols()
    mismatch = next(e for e in audit.events if e["event_type"] == "PositionQuantityMismatch")
    assert mismatch["payload"]["severity"] == "critical"


# ── H: protection tracks the remaining position ──────────────────────────────


class _Resizer:
    """Stand-in for ExecutionService's protection interface."""

    def __init__(self) -> None:
        self.resized: list[tuple[str, Decimal]] = []
        self.installed: list[tuple[str, Decimal]] = []
        self.cancelled: list[tuple[str, str]] = []

    async def ensure_protection(
        self,
        *,
        symbol: str,
        qty: Decimal,
        stop_price: Decimal,
        position_id: object = None,
        reason: str,
    ) -> str | None:
        self.installed.append((symbol, qty))
        return "new-stop"

    async def resize_protection(
        self,
        *,
        symbol: str,
        position_id: object,
        remaining_qty: Decimal,
        stop_price: Decimal | None,
        reason: str,
        previous_stop_order_id: str | None = None,
    ) -> str | None:
        self.resized.append((symbol, remaining_qty))
        return "resized-stop"

    async def cancel_protection(
        self,
        *,
        broker_order_id: str,
        symbol: str,
        reason: str,
    ) -> bool:
        self.cancelled.append((symbol, broker_order_id))
        return True


async def test_a_stop_larger_than_the_position_is_repaired(ledger: PositionLedger) -> None:
    """A 100-share stop against 70 shares would sell 30 we do not own."""
    _position(ledger, qty=Decimal(70), stop_order_id="stale-stop")
    stale = _sell(
        oid="stale-stop",
        status=OrderStatus.ACCEPTED,
        qty=Decimal(100),
        filled=None,
        order_type=OrderType.STOP,
    )
    execution = _Resizer()
    audit = InMemoryAudit()

    await reconcile_protective_orders(
        _Broker(open_orders=[stale]),  # type: ignore[arg-type]
        ledger,
        audit,
        execution=execution,  # type: ignore[arg-type]
    )

    assert execution.resized == [("AAPL", Decimal(70))]
    mismatch = next(e for e in audit.events if e["event_type"] == "ProtectionQuantityMismatch")
    assert mismatch["payload"]["severity"] == "critical"


async def test_a_correctly_sized_stop_is_left_alone(ledger: PositionLedger) -> None:
    _position(ledger, qty=Decimal(70), stop_order_id="good-stop")
    good = _sell(
        oid="good-stop",
        status=OrderStatus.ACCEPTED,
        qty=Decimal(70),
        filled=None,
        order_type=OrderType.STOP,
    )
    execution = _Resizer()

    await reconcile_protective_orders(
        _Broker(open_orders=[good]),  # type: ignore[arg-type]
        ledger,
        InMemoryAudit(),
        execution=execution,  # type: ignore[arg-type]
    )

    assert execution.resized == []
    assert execution.installed == []


# ── Protection is external state, so failing to read it is not "fine" ────────


class _UnreadableBroker(_Broker):
    async def list_open_orders(self) -> list[OrderRecord]:
        raise RuntimeError("gateway dropped the session")


async def test_unreadable_open_orders_leave_protection_unverified_not_assumed(
    ledger: PositionLedger,
) -> None:
    """A stop we could not look at is not a stop we confirmed.

    The protective order lives at the broker, so the only evidence it still
    exists is a successful read. When that read fails the honest state is
    "unknown", and it has to be loud: silence here reads exactly like a clean
    pass, which is how an unprotected position gets left alone for hours.
    """
    _position(ledger, qty=Decimal(70), stop_order_id="stop-1")
    audit = InMemoryAudit()
    report = ReconciliationReport()
    execution = _Resizer()

    restored = await reconcile_protective_orders(
        _UnreadableBroker(),  # type: ignore[arg-type]
        ledger,
        audit,
        execution=execution,  # type: ignore[arg-type]
        report=report,
    )

    assert restored == 0
    assert execution.installed == [], "never re-place a stop that may already be resting"
    assert report.severity == "critical"
    assert "protection:AAPL:unverified" in report.unresolved
    event = next(e for e in audit.events if e["event_type"] == "ProtectionUnverified")
    assert event["payload"]["symbol"] == "AAPL"
    assert event["payload"]["severity"] == "critical"


async def test_every_exposed_position_is_named_when_protection_cannot_be_read(
    ledger: PositionLedger,
) -> None:
    """One generic line does not tell an operator how much is exposed."""
    _position(ledger, qty=Decimal(70), stop_order_id="stop-1")
    report = ReconciliationReport()

    await reconcile_protective_orders(
        _UnreadableBroker(),  # type: ignore[arg-type]
        ledger,
        InMemoryAudit(),
        report=report,
    )

    unverified = [u for u in report.unresolved if u.startswith("protection:")]
    assert unverified == ["protection:AAPL:unverified"]
    assert all("broker_unreadable" not in u for u in report.unresolved), (
        "the report must name the exposed symbol, not just the failed call"
    )
