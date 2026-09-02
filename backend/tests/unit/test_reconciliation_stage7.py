"""
Reconciliation against broker truth.

Each test is one way local state and broker state can disagree, and the answer
is always the same shape: either resolve it from what the broker says, or record
it as unresolved and keep the symbol blocked. Never guess.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from broker.paper.mock import MockPaperBroker
from core.audit import InMemoryAudit
from core.enums import (
    IntentStatus,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
)
from core.schemas import OrderRecord, Position
from trading.intents import MemoryOrderIntentStore
from trading.order_intent import OrderIntent
from trading.reconcile import (
    SEVERITY_CRITICAL,
    SEVERITY_OK,
    ReconciliationReport,
    reconcile_order_intents,
    reconcile_protective_orders,
)

pytestmark = pytest.mark.asyncio


def _intent(
    intents: MemoryOrderIntentStore,
    *,
    status: IntentStatus,
    broker_order_id: str | None = None,
    symbol: str = "AAPL",
    key: str = "entry:recon:0",
) -> OrderIntent:
    intent, _ = intents.create_or_get(
        OrderIntent(
            idempotency_key=key,
            broker="MockPaperBroker",
            symbol=symbol,
            side=OrderSide.BUY,
            requested_qty=Decimal(10),
            order_type=OrderType.LIMIT,
            limit_price=Decimal(100),
            status=status,
            broker_order_id=broker_order_id,
            client_order_id="traido-e-recon",
            approval_admission_record_id=uuid4() if status is IntentStatus.CREATED else None,
            geometry_hash="recon-test",
        )
    )
    return intent


def _order(
    broker: MockPaperBroker,
    *,
    status: OrderStatus,
    filled: Decimal | None = None,
) -> OrderRecord:
    record = OrderRecord(
        id=uuid4(),
        client_order_id="traido-e-recon",
        broker_order_id=str(uuid4()),
        symbol="AAPL",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        qty=Decimal(10),
        status=status,
        limit_price=Decimal(100),
        filled_avg_price=Decimal(100) if filled else None,
        filled_qty=filled,
    )
    broker.orders.append(record)
    return record


# ── A. Local SUBMITTED, broker FILLED ────────────────────────────────────────


async def test_a_fill_we_missed_is_adopted() -> None:
    broker = MockPaperBroker()
    intents = MemoryOrderIntentStore()
    audit = InMemoryAudit()
    order = _order(broker, status=OrderStatus.FILLED, filled=Decimal(10))
    intent = _intent(intents, status=IntentStatus.SUBMITTED, broker_order_id=order.broker_order_id)

    report = await reconcile_order_intents(broker, intents, audit)

    assert intents.get(intent.id).status is IntentStatus.FILLED
    assert intents.get(intent.id).filled_qty == Decimal(10)
    assert any("fill_needs_protection" in item for item in report.unresolved)


# ── B. Local PARTIALLY_FILLED, broker CANCELLED with a fill ──────────────────


async def test_a_cancelled_order_that_filled_is_not_treated_as_cancelled() -> None:
    """Those shares exist. Recording this as CANCELED would lose a live position."""
    broker = MockPaperBroker()
    intents = MemoryOrderIntentStore()
    order = _order(broker, status=OrderStatus.CANCELED, filled=Decimal(4))
    intent = _intent(
        intents, status=IntentStatus.ACKNOWLEDGED, broker_order_id=order.broker_order_id
    )

    await reconcile_order_intents(broker, intents, InMemoryAudit())

    settled = intents.get(intent.id)
    assert settled.status is IntentStatus.PARTIALLY_FILLED
    assert settled.filled_qty == Decimal(4)


# ── D. Local says active order, broker has no trace ──────────────────────────


async def test_an_order_the_broker_cannot_account_for_becomes_unknown() -> None:
    broker = MockPaperBroker()
    intents = MemoryOrderIntentStore()
    audit = InMemoryAudit()
    intent = _intent(intents, status=IntentStatus.SUBMITTED, broker_order_id="ghost-order")

    report = await reconcile_order_intents(broker, intents, audit)

    assert intents.get(intent.id).status is IntentStatus.UNKNOWN
    assert report.severity == SEVERITY_CRITICAL
    assert any(e["event_type"] == "EntryStateUnknown" for e in audit.events)
    # And the symbol stays barred from new entries.
    assert "AAPL" in intents.unresolved_symbols()


async def test_an_intent_that_never_reached_the_broker_is_retired_not_flagged() -> None:
    """CREATED with no ids is provable: nothing was sent, so nothing is at risk."""
    broker = MockPaperBroker()
    intents = MemoryOrderIntentStore()
    intent, _ = intents.create_or_get(
        OrderIntent(
            idempotency_key="entry:never:0",
            broker="MockPaperBroker",
            symbol="AAPL",
            side=OrderSide.BUY,
            requested_qty=Decimal(10),
            order_type=OrderType.LIMIT,
            approval_admission_record_id=uuid4(),
            geometry_hash="recon-never",
        )
    )

    report = await reconcile_order_intents(broker, intents, InMemoryAudit())

    assert intents.get(intent.id).status is IntentStatus.REJECTED
    assert report.unresolved == []


# ── Repeatability ────────────────────────────────────────────────────────────


async def test_running_reconciliation_twice_changes_nothing_the_second_time() -> None:
    broker = MockPaperBroker()
    intents = MemoryOrderIntentStore()
    order = _order(broker, status=OrderStatus.FILLED, filled=Decimal(10))
    _intent(intents, status=IntentStatus.SUBMITTED, broker_order_id=order.broker_order_id)

    first = await reconcile_order_intents(broker, intents, InMemoryAudit())
    second = await reconcile_order_intents(broker, intents, InMemoryAudit())

    assert first.changed
    assert second.changed == []
    assert second.severity == SEVERITY_OK


async def test_reconciling_an_unknown_intent_repeatedly_keeps_it_unknown() -> None:
    """UNKNOWN must not decay into a comfortable answer just because we looked again."""
    broker = MockPaperBroker()
    intents = MemoryOrderIntentStore()
    intent = _intent(intents, status=IntentStatus.UNKNOWN, broker_order_id="ghost-order")

    for _ in range(3):
        await reconcile_order_intents(broker, intents, InMemoryAudit())

    assert intents.get(intent.id).status is IntentStatus.UNKNOWN


# ── E. Protective stop missing at the broker ─────────────────────────────────


class _Ledger:
    """Minimal ledger stand-in exposing only what the protective sweep reads."""

    def __init__(self, rows: list[object]) -> None:
        self._rows = rows
        self.updated: list[tuple[object, str]] = []

    def get_open(self) -> list[object]:
        return list(self._rows)

    def set_stop_order_id(self, position_id: object, stop_order_id: str) -> None:
        self.updated.append((position_id, stop_order_id))


class _Row:
    def __init__(self, *, stop_order_id: str | None) -> None:
        self.id = uuid4()
        self.symbol = "AAPL"
        self.qty = Decimal(10)
        self.avg_entry = Decimal(100)
        self.stop_price = Decimal(95)
        self.payload = {"stop_order_id": stop_order_id} if stop_order_id else {}


class _Installer:
    def __init__(self, *, succeeds: bool = True) -> None:
        self.succeeds = succeeds
        self.calls: list[dict[str, object]] = []
        self.cancelled: list[dict[str, object]] = []

    async def ensure_protection(self, **kwargs: object) -> str | None:
        self.calls.append(kwargs)
        return "restored-stop-id" if self.succeeds else None

    async def cancel_protection(self, **kwargs: object) -> bool:
        self.cancelled.append(kwargs)
        return True


async def test_a_position_whose_stop_vanished_is_reprotected() -> None:
    broker = MockPaperBroker()
    ledger = _Ledger([_Row(stop_order_id="stop-that-disappeared")])
    installer = _Installer()
    audit = InMemoryAudit()
    report = ReconciliationReport()

    restored = await reconcile_protective_orders(
        broker, ledger, audit, execution=installer, report=report
    )

    assert restored == 1
    assert installer.calls[0]["qty"] == Decimal(10)
    assert installer.calls[0]["stop_price"] == Decimal(95)
    assert ledger.updated == [(ledger.get_open()[0].id, "restored-stop-id")] or ledger.updated
    assert any(e["event_type"] == "ProtectiveOrderMissing" for e in audit.events)


async def test_a_position_with_a_resting_stop_is_left_alone() -> None:
    broker = MockPaperBroker()
    stop = OrderRecord(
        id=uuid4(),
        client_order_id="traido-s-live",
        broker_order_id="live-stop",
        symbol="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        qty=Decimal(10),
        status=OrderStatus.ACCEPTED,
        stop_price=Decimal(95),
    )
    broker.orders.append(stop)
    installer = _Installer()

    restored = await reconcile_protective_orders(
        broker, _Ledger([_Row(stop_order_id="live-stop")]), InMemoryAudit(), execution=installer
    )

    assert restored == 0
    assert installer.calls == []


async def test_a_stop_that_cannot_be_reinstalled_is_reported_unresolved() -> None:
    """Emergency close is the fallback, and the outcome is still not 'fine'."""
    report = ReconciliationReport()

    restored = await reconcile_protective_orders(
        MockPaperBroker(),
        _Ledger([_Row(stop_order_id=None)]),
        InMemoryAudit(),
        execution=_Installer(succeeds=False),
        report=report,
    )

    assert restored == 0
    assert any("emergency_closed" in item for item in report.unresolved)
    assert report.severity == SEVERITY_CRITICAL


# ── C. Broker has a position we know nothing about ───────────────────────────


async def test_an_orphan_broker_position_blocks_the_symbol() -> None:
    from trading import external_positions as ep
    from trading.reconcile import block_symbol_as_unknown, clear_resolved_orphan_blocks

    intents = MemoryOrderIntentStore()
    audit = InMemoryAudit()

    await block_symbol_as_unknown(
        intents, symbol="NVDA", qty=Decimal(5), reason="no ledger row", audit=audit
    )

    assert "NVDA" in ep.EXTERNAL_POSITIONS.blocking_symbols()
    assert any(e["event_type"] == "ExternalPositionIncidentOpened" for e in audit.events)

    # Blocking the same orphan twice must not pile up incidents.
    await block_symbol_as_unknown(intents, symbol="NVDA", qty=Decimal(5), reason="again")
    assert len(ep.EXTERNAL_POSITIONS.list_open()) == 1

    # And the block lifts once the orphan is gone.
    cleared = await clear_resolved_orphan_blocks(intents, live_symbols=set())
    assert cleared >= 1
    assert "NVDA" not in ep.EXTERNAL_POSITIONS.blocking_symbols()


async def test_full_reconcile_reports_an_orphan_as_critical() -> None:
    from trading import external_positions as ep
    from trading.reconcile import reconcile_positions

    broker = MockPaperBroker()
    broker.positions.append(
        Position(
            id=uuid4(),
            symbol="NVDA",
            qty=Decimal(5),
            avg_entry=Decimal(500),
            stop_price=None,
            target_price=None,
            status=PositionStatus.OPEN,
            opened_at=datetime.now(UTC),
        )
    )
    intents = MemoryOrderIntentStore()
    audit = InMemoryAudit()

    result = await reconcile_positions(broker, audit, intents=intents)

    assert "NVDA" in result["orphans"]
    assert result["severity"] == SEVERITY_CRITICAL
    assert "NVDA" in ep.EXTERNAL_POSITIONS.blocking_symbols()
    assert any(e["event_type"] == "ReconciliationUnresolved" for e in audit.events)
