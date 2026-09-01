"""
Reconcile Traido's durable state against broker truth.

The broker is authoritative for execution state; this module's job is to make
local state agree with it, or to say plainly that it cannot. Every path here
ends in one of two places — a resolved fact, or an explicitly unresolved item
that blocks conflicting trading. Guessing is not one of the options.

Safe to run repeatedly: every step is keyed on current state, so a second pass
over an already-consistent book changes nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from core.activity import BOARD
from core.enums import IntentStatus, OpportunityStatus, OrderSide, OrderType
from core.ports import AuditPort, BrokerPort
from core.schemas import OrderRecord
from trading.intents import INTENTS, OrderIntentStorePort, apply_exit_to_ledger
from trading.ledger import LEDGER, PositionLedger
from trading.order_intent import OrderIntent, intent_status_for, locate_broker_order

logger = logging.getLogger(__name__)

SEVERITY_OK = "ok"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

_RESOLUTION_EVENTS: dict[IntentStatus, str] = {
    IntentStatus.FILLED: "OrderFilled",
    IntentStatus.PARTIALLY_FILLED: "OrderPartiallyFilled",
    IntentStatus.CANCELED: "OrderCancelled",
    IntentStatus.REJECTED: "OrderRejected",
    IntentStatus.EXPIRED: "OrderExpired",
    IntentStatus.ACKNOWLEDGED: "OrderAcknowledged",
}
"""Name the outcome, not just the fact that reconciliation ran."""

_EXIT_RESOLUTION_EVENTS: dict[IntentStatus, str] = {
    IntentStatus.FILLED: "ExitFilled",
    IntentStatus.PARTIALLY_FILLED: "ExitPartiallyFilled",
    IntentStatus.CANCELED: "ExitCancelled",
    IntentStatus.REJECTED: "ExitRejected",
    IntentStatus.EXPIRED: "OrderExpired",
    IntentStatus.ACKNOWLEDGED: "ExitAcknowledged",
}


@dataclass
class ReconciliationReport:
    """What was inspected, what moved, and what is still not known."""

    checked: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    @property
    def severity(self) -> str:
        if self.unresolved:
            return SEVERITY_CRITICAL
        return SEVERITY_WARNING if self.changed else SEVERITY_OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "checked": list(self.checked),
            "changed": list(self.changed),
            "unresolved": list(self.unresolved),
            "severity": self.severity,
        }


class ProtectionInstaller(Protocol):
    """Narrow view of ExecutionService that reconciliation is allowed to use.

    Reconciliation must never place orders itself — the execution service stays
    the single path to the broker — so it hands the job back through this.

    Structural rather than nominal: the point is to name the four operations
    reconciliation may ask for, not to make anything inherit from a base class.
    A nominal version would also have to be satisfied by every test stub, which
    is how a "narrow view" quietly becomes a second copy of the service.
    """

    async def ensure_protection(
        self,
        *,
        symbol: str,
        qty: Decimal,
        stop_price: Decimal,
        position_id: Any = None,
        reason: str,
    ) -> str | None: ...

    async def resize_protection(
        self,
        *,
        symbol: str,
        position_id: Any,
        remaining_qty: Decimal,
        stop_price: Decimal | None,
        reason: str,
        previous_stop_order_id: str | None = None,
    ) -> str | None: ...

    async def cancel_protection(
        self,
        *,
        broker_order_id: str,
        symbol: str,
        reason: str,
    ) -> bool: ...

    async def cancel_entry_order(
        self,
        *,
        broker_order_id: str,
        symbol: str,
        reason: str,
    ) -> bool: ...


async def reconcile_positions(
    broker: BrokerPort,
    audit: AuditPort | None = None,
    *,
    ledger: PositionLedger | None = None,
    intents: OrderIntentStorePort | None = None,
    execution: ProtectionInstaller | None = None,
) -> dict[str, Any]:
    """
    - Unresolved order intents → resolved against broker truth, or marked UNKNOWN.
    - Ledger open + broker flat → journal as broker-closed (stop/external).
    - Broker open + no ledger → orphan: audited and blocked until explained.
    - Ledger open + protective stop missing → reinstalled, or emergency-closed.
    - Resting Traido entry buys with no in-flight APPROVING → cancel (fill-timeout leftovers).
    """
    store = ledger or LEDGER
    intent_store: OrderIntentStorePort = intents if intents is not None else INTENTS
    report = ReconciliationReport()
    BOARD.set_agent("position", status="working", detail="Reconciling ledger↔broker")

    if audit:
        await audit.append("ReconciliationStarted", "reconcile", {})

    await reconcile_order_intents(broker, intent_store, audit, report=report)

    broker_pos = await broker.list_positions()
    by_sym = {p.symbol.upper(): p for p in broker_pos}

    closed = 0
    orphans: list[str] = []

    # Quantities first: the sweeps below decide what to journal, and journalling
    # a size the broker disagrees with would bake the discrepancy into history.
    await reconcile_position_quantities(store, intent_store, by_sym, audit, report=report)

    for row in store.get_open():
        sym = row.symbol.upper()
        report.checked.append(f"position:{sym}")
        if sym in by_sym:
            continue
        report.changed.append(f"position:{sym}:closed_broker_flat")
        # Position vanished at broker — close journal at last known entry (0 PnL) unless
        # we have a better mark in payload; use entry as conservative unknown exit.
        exit_px = Decimal(str(row.avg_entry))
        journal = store.close_and_journal(
            symbol=sym,
            exit_price=exit_px,
            exit_reasons=["Reconcile: broker flat (stop or external close)"],
            qty=Decimal(str(row.qty)),
        )
        closed += 1
        if audit and journal:
            await audit.append(
                "PositionReconciledClosed",
                "reconcile",
                {
                    "symbol": sym,
                    "journal_id": str(journal.id),
                    "note": "broker_flat",
                },
                entity_type="journal",
                entity_id=str(journal.id),
            )
            await audit.append(
                "TradeJournalFinalized",
                "reconcile",
                {"journal_id": str(journal.id), "pnl": str(journal.pnl), "source": "reconcile"},
                entity_type="journal",
                entity_id=str(journal.id),
            )

    ledger_syms = {r.symbol.upper() for r in store.get_open()}
    for sym, pos in by_sym.items():
        if sym not in ledger_syms:
            orphans.append(sym)
            report.unresolved.append(f"orphan_position:{sym}")
            # We hold something we cannot explain. Block the symbol rather than
            # inventing a ledger row for a position with unknown provenance.
            await block_symbol_as_unknown(
                intent_store,
                symbol=sym,
                qty=Decimal(str(pos.qty)),
                reason="broker position with no ledger row",
                audit=audit,
            )
            if audit:
                await audit.append(
                    "OrphanBrokerPosition",
                    "reconcile",
                    {"symbol": sym, "qty": str(pos.qty), "avg_entry": str(pos.avg_entry)},
                    entity_type="position",
                    entity_id=sym,
                )
    await clear_resolved_orphan_blocks(intent_store, live_symbols=set(by_sym), report=report)

    protection = await reconcile_protective_orders(
        broker, store, audit, execution=execution, report=report
    )

    stale = 0
    try:
        from trading.opportunities import OPPORTUNITIES, withdraw_unactionable

        if hasattr(OPPORTUNITIES, "release_stale_approving"):
            stale = OPPORTUNITIES.release_stale_approving(older_than_sec=90.0)
        # Also swept at the top of a scan cycle, which is what makes the slot
        # accounting right. Here as well because a cycle is five minutes and
        # this pass is thirty seconds: the same hygiene, at the rate the
        # operator is actually watching the screen.
        withdraw_unactionable(OPPORTUNITIES)
    except Exception:  # noqa: BLE001
        stale = 0

    canceled_entries = await cancel_orphaned_entry_orders(broker, audit, execution=execution)

    detail = (
        f"reconcile · closed {closed} · orphans {len(orphans)} · "
        f"canceled_entries {canceled_entries} · stale_approving {stale} · {report.severity}"
    )
    BOARD.set_agent("position", status="done", detail=detail)
    BOARD.log("position", detail)

    if audit:
        event = "ReconciliationUnresolved" if report.unresolved else "ReconciliationResolved"
        await audit.append(event, "reconcile", report.as_dict())

    return {
        "closed": closed,
        "orphans": orphans,
        "broker_open": len(broker_pos),
        "canceled_entries": canceled_entries,
        "protection_restored": protection,
        **report.as_dict(),
    }


# ── Order intents ────────────────────────────────────────────────────────────


async def reconcile_order_intents(
    broker: BrokerPort,
    intents: OrderIntentStorePort,
    audit: AuditPort | None = None,
    *,
    report: ReconciliationReport | None = None,
) -> ReconciliationReport:
    """Settle every unresolved intent against what the broker says.

    An intent the broker cannot account for becomes UNKNOWN and stays that way.
    That is the honest answer, and it keeps the symbol blocked until a human or
    a later pass can explain it.
    """
    rep = report if report is not None else ReconciliationReport()

    for intent in intents.list_unresolved():
        if intent.idempotency_key.startswith(_ORPHAN_PREFIX):
            continue  # handled by the position sweep, which owns their lifetime
        rep.checked.append(f"intent:{intent.id}")

        if intent.may_resubmit:
            # Never transmitted, so nothing to reconcile — retire it.
            intents.transition(intent.id, IntentStatus.REJECTED, last_error="never submitted")
            rep.changed.append(f"intent:{intent.id}:retired")
            continue

        found = await locate_broker_order(broker, intent)
        if found is None:
            await _mark_intent_unknown(intents, audit, intent, "broker has no trace of this order")
            rep.unresolved.append(f"intent:{intent.id}:no_broker_trace")
            continue

        target = intent_status_for(found.status, found.filled_qty)
        if target is intent.status:
            intents.update_fields(intent.id, last_broker_state=found.status.value)
            # The status has not moved, but a working exit keeps filling under
            # it: PARTIALLY_FILLED at 40 and at 70 are the same status and two
            # different positions. Absorbing the difference here is what stops
            # the book drifting away from the broker while the order rests.
            if intent.is_exit and target is IntentStatus.PARTIALLY_FILLED:
                intents.update_fields(intent.id, filled_qty=found.filled_qty or intent.filled_qty)
                await _absorb_exit_fill(intents, audit, intent, found, rep)
            continue

        try:
            intents.transition(
                intent.id,
                target,
                broker_order_id=found.broker_order_id,
                filled_qty=found.filled_qty or intent.filled_qty,
                average_fill_price=found.filled_avg_price or intent.average_fill_price,
                last_broker_state=found.status.value,
            )
        except RuntimeError:
            # The lifecycle forbids this move, which means our model of the
            # order is wrong. That is precisely what UNKNOWN is for.
            await _mark_intent_unknown(
                intents, audit, intent, f"broker reports {found.status.value}, local disagrees"
            )
            rep.unresolved.append(f"intent:{intent.id}:illegal_transition")
            continue

        rep.changed.append(f"intent:{intent.id}:{target.value}")
        if audit:
            await audit.append(
                _resolution_event(intent, target),
                "reconcile",
                {
                    "intent_id": str(intent.id),
                    "purpose": intent.purpose.value,
                    "from": intent.status.value,
                    "to": target.value,
                    "broker_order_id": found.broker_order_id,
                    "filled_qty": str(found.filled_qty or 0),
                },
                entity_type="order_intent",
                entity_id=str(intent.id),
            )

        if target not in {IntentStatus.PARTIALLY_FILLED, IntentStatus.FILLED}:
            continue

        if intent.is_exit:
            # An exit that filled while we were not looking has already changed
            # our exposure. The book has to absorb it before anything else can
            # reason about position size.
            await _absorb_exit_fill(intents, audit, intent, found, rep)
        else:
            # A fill without a protected position is still an open problem; the
            # protective sweep below decides what to do about it.
            rep.unresolved.append(f"intent:{intent.id}:fill_needs_protection")

    return rep


def _resolution_event(intent: OrderIntent, target: IntentStatus) -> str:
    table = _EXIT_RESOLUTION_EVENTS if intent.is_exit else _RESOLUTION_EVENTS
    return table.get(target, "OrderStateReconciled")


async def _absorb_exit_fill(
    intents: OrderIntentStorePort,
    audit: AuditPort | None,
    intent: OrderIntent,
    found: OrderRecord,
    rep: ReconciliationReport,
) -> None:
    """Apply a recovered exit fill to the ledger exactly once."""
    filled = found.filled_qty or Decimal(0)
    if filled <= 0:
        return

    price = found.filled_avg_price or intent.average_fill_price
    if price is None or price <= 0:
        # We know shares left but not at what price. Guessing a price would
        # write a fictional PnL into the journal, so this stays open.
        rep.unresolved.append(f"intent:{intent.id}:exit_fill_price_unknown")
        return

    applied = apply_exit_to_ledger(
        intents,
        intent.model_copy(update={"filled_qty": filled}),
        filled_qty=filled,
        exit_price=price,
        reasons=["Reconcile: exit fill recovered from broker"],
    )
    if applied.filled_qty <= 0:
        return

    rep.changed.append(f"exit_fill:{intent.symbol}:{applied.filled_qty}")
    if audit:
        await audit.append(
            "ExitFillReconciled",
            "reconcile",
            {
                "intent_id": str(intent.id),
                "symbol": intent.symbol,
                "applied_qty": str(applied.filled_qty),
                "remaining_qty": str(applied.remaining_qty),
                "closed": applied.closed,
            },
            entity_type="order_intent",
            entity_id=str(intent.id),
        )
    if applied.remaining_qty > 0:
        # Smaller position, and its old stop no longer matches. The protective
        # sweep resizes it; flagging it here is what makes that sweep look.
        rep.unresolved.append(f"protection:{intent.symbol}:needs_resize")


async def _mark_intent_unknown(
    intents: OrderIntentStorePort,
    audit: AuditPort | None,
    intent: OrderIntent,
    reason: str,
) -> None:
    if intent.status is not IntentStatus.UNKNOWN:
        intents.transition(intent.id, IntentStatus.UNKNOWN, last_error=reason)
    if audit:
        await audit.append(
            "ExitStateUnknown" if intent.is_exit else "EntryStateUnknown",
            "reconcile",
            {"intent_id": str(intent.id), "symbol": intent.symbol, "note": reason},
            entity_type="order_intent",
            entity_id=str(intent.id),
        )


# ── Position quantities ──────────────────────────────────────────────────────


async def reconcile_position_quantities(
    ledger: PositionLedger,
    intents: OrderIntentStorePort,
    broker_positions: dict[str, Any],
    audit: AuditPort | None = None,
    *,
    report: ReconciliationReport | None = None,
) -> int:
    """Make local size agree with broker size, but only when we can explain it.

    A local 100 against a broker 40 has exactly one benign explanation: exits we
    know about filled 60. If recorded exit fills account for the gap, the book
    is corrected. If they do not, something moved these shares that Traido did
    not do, and inventing a number to paper over that would be worse than
    stopping — so the symbol is blocked instead.
    """
    rep = report if report is not None else ReconciliationReport()
    adjusted = 0

    for row in ledger.get_open():
        sym = row.symbol.upper()
        pos = broker_positions.get(sym)
        if pos is None:
            continue  # the flat-at-broker sweep owns this case

        local = Decimal(str(row.qty))
        actual = Decimal(str(pos.qty))
        rep.checked.append(f"quantity:{sym}")
        if local == actual:
            continue

        explained = _exit_fills_for(intents, position_id=row.id, symbol=sym)
        deterministic = local - actual == explained and actual >= 0

        if not deterministic:
            rep.unresolved.append(f"quantity:{sym}:local={local}:broker={actual}")
            await block_symbol_as_unknown(
                intents,
                symbol=sym,
                qty=actual,
                reason=f"position size disagreement: local {local}, broker {actual}",
                audit=audit,
            )
            if audit:
                await audit.append(
                    "PositionQuantityMismatch",
                    "reconcile",
                    {
                        "symbol": sym,
                        "position_id": str(row.id),
                        "local_qty": str(local),
                        "broker_qty": str(actual),
                        "explained_by_exit_fills": str(explained),
                        "severity": SEVERITY_CRITICAL,
                    },
                    entity_type="position",
                    entity_id=str(row.id),
                )
            continue

        ledger.set_quantity(row.id, actual)
        adjusted += 1
        rep.changed.append(f"quantity:{sym}:{local}->{actual}")
        if audit:
            await audit.append(
                "PositionQuantityReconciled",
                "reconcile",
                {
                    "symbol": sym,
                    "position_id": str(row.id),
                    "from_qty": str(local),
                    "to_qty": str(actual),
                    "explained_by_exit_fills": str(explained),
                },
                entity_type="position",
                entity_id=str(row.id),
            )
    return adjusted


def _exit_fills_for(
    intents: OrderIntentStorePort,
    *,
    position_id: Any,
    symbol: str,
) -> Decimal:
    """Total quantity that recorded exit intents actually sold for this position."""
    total = Decimal(0)
    for intent in intents.list_by_key_prefix("exit:"):
        if intent.position_id == position_id:
            total += intent.filled_qty
    for intent in intents.list_by_key_prefix("emergency_exit:"):
        if intent.position_id == position_id or (
            intent.position_id is None and intent.symbol.upper() == symbol
        ):
            total += intent.filled_qty
    return total


# ── Orphan positions ─────────────────────────────────────────────────────────

_ORPHAN_PREFIX = "orphan:"


async def block_symbol_as_unknown(
    intents: OrderIntentStorePort,
    *,
    symbol: str,
    qty: Decimal,
    reason: str,
    audit: AuditPort | None = None,
) -> OrderIntent:
    """Record an UNKNOWN intent so the symbol is barred from new entries.

    Reusing the intent store here means orphan positions block through exactly
    the same mechanism as ambiguous orders, rather than a second parallel one.
    """
    from core.enums import OrderSide as _Side
    from core.enums import OrderType as _Type

    ticker = symbol.upper()
    prefix = f"{_ORPHAN_PREFIX}{ticker}:"
    existing = intents.list_by_key_prefix(prefix)
    live = next((i for i in existing if i.is_unresolved), None)
    if live is not None:
        return live

    intent, created = intents.create_or_get(
        OrderIntent(
            idempotency_key=f"{prefix}{len(existing)}",
            broker="unknown",
            symbol=ticker,
            side=_Side.BUY,
            requested_qty=qty,
            order_type=_Type.MARKET,
            status=IntentStatus.UNKNOWN,
            last_error=reason,
        )
    )
    if created and audit:
        await audit.append(
            "EntryStateUnknown",
            "reconcile",
            {"symbol": ticker, "intent_id": str(intent.id), "note": reason},
            entity_type="order_intent",
            entity_id=str(intent.id),
        )
    return intent


async def clear_resolved_orphan_blocks(
    intents: OrderIntentStorePort,
    *,
    live_symbols: set[str],
    report: ReconciliationReport | None = None,
) -> int:
    """Release symbols whose orphan position is gone. Blocks must be able to lift."""
    cleared = 0
    for intent in intents.list_unresolved():
        if not intent.idempotency_key.startswith(_ORPHAN_PREFIX):
            continue
        if intent.symbol.upper() in live_symbols:
            continue
        intents.transition(intent.id, IntentStatus.CANCELED, last_error="orphan position gone")
        cleared += 1
        if report is not None:
            report.changed.append(f"orphan_block:{intent.symbol}:cleared")
    return cleared


# ── Protective orders ────────────────────────────────────────────────────────


async def reconcile_protective_orders(
    broker: BrokerPort,
    ledger: PositionLedger,
    audit: AuditPort | None = None,
    *,
    execution: ProtectionInstaller | None = None,
    report: ReconciliationReport | None = None,
) -> int:
    """Re-verify every open position's stop against the broker, every pass.

    A protective order is external state. We do not own it, we cannot enforce
    it, and having placed it once is not evidence that it is still there: it can
    be cancelled at the venue, resized by a partial exit, or lost with an
    account change. So its existence and size are read back rather than
    remembered, and a position whose stop has vanished is treated as a naked
    long — re-protected, or handed to the emergency-close path.

    Note what this does *not* establish. That an order object exists says
    nothing about whether it will actually trigger: with IBKR in particular a
    stop may be simulated rather than native, in which case the trigger belongs
    to IB's systems and its behaviour varies by venue, product and session. This
    check confirms the order is present and correctly sized. It cannot confirm
    the venue will fire it, which is why the emergency-close path exists and why
    a resting stop never ends an incident on its own.
    """
    rep = report if report is not None else ReconciliationReport()
    restored = 0

    try:
        open_orders = await broker.list_open_orders()
    except Exception:
        logger.warning("reconcile: cannot list open orders for protection audit", exc_info=True)
        # Not "the stops are fine" — we did not look. Every open position is
        # now of unknown protection status and each one is named, because one
        # generic line does not tell an operator how much is exposed.
        await _report_protection_unverified(ledger, audit, rep)
        return 0

    resting = {
        o.broker_order_id: o for o in open_orders if o.side == OrderSide.SELL and o.broker_order_id
    }

    held_at_broker = await _broker_quantities(broker)

    for row in ledger.get_open():
        payload = row.payload or {}
        stop_oid = payload.get("stop_order_id")
        stop_price = row.stop_price
        rep.checked.append(f"protection:{row.symbol}")

        # Sized against broker truth, not the ledger row. The two agree almost
        # always, and the exception is the case that matters: when
        # `reconcile_position_quantities` has just found a disagreement it
        # cannot explain, it deliberately leaves the local number alone and
        # blocks the symbol. Sizing protection from that number then compares a
        # stop for 50 against a book of 50, calls it correct, and leaves a
        # resting SELL for 50 above the 25 shares the venue actually holds.
        #
        # The smaller of the two is the only safe figure: a stop under the
        # position leaves part of it naked, which reconciliation will restore on
        # this same pass, while a stop over the position sells shares that do
        # not exist.
        local = Decimal(str(row.qty))
        actual = held_at_broker.get(row.symbol.upper())
        held = local if actual is None else min(local, actual)

        stop = resting.get(str(stop_oid)) if stop_oid else None
        if stop is not None and stop.qty == held:
            continue

        if stop is not None:
            # The stop exists but covers the wrong size. Too large is the
            # dangerous direction — it would sell shares we no longer own — so
            # this is repaired rather than merely reported.
            await _resize_stale_protection(
                ledger, audit, execution, row, stop=stop, held=held, rep=rep
            )
            continue

        if audit:
            await audit.append(
                "ProtectiveOrderMissing",
                "reconcile",
                {
                    "symbol": row.symbol,
                    "position_id": str(row.id),
                    "qty": str(held),
                    "expected_stop_order_id": str(stop_oid) if stop_oid else None,
                },
                entity_type="position",
                entity_id=str(row.id),
            )

        if execution is None or stop_price is None:
            rep.unresolved.append(f"protection:{row.symbol}:missing")
            continue

        new_oid = await execution.ensure_protection(
            symbol=row.symbol,
            qty=held,
            stop_price=Decimal(str(stop_price)),
            position_id=row.id,
            reason="reconcile: protective stop missing at broker",
        )
        if new_oid is None:
            rep.unresolved.append(f"protection:{row.symbol}:emergency_closed")
            continue
        ledger.set_stop_order_id(row.id, new_oid)
        restored += 1
        rep.changed.append(f"protection:{row.symbol}:restored")

    await cancel_excess_protection(
        broker,
        ledger,
        audit,
        open_orders=open_orders,
        held_at_broker=held_at_broker,
        execution=execution,
        report=rep,
    )
    return restored


async def _broker_quantities(broker: BrokerPort) -> dict[str, Decimal]:
    """What the venue says it holds, by symbol. Empty when it cannot be read."""
    try:
        return {p.symbol.upper(): Decimal(str(p.qty)) for p in await broker.list_positions()}
    except Exception:
        logger.warning(
            "reconcile: cannot read broker positions for protection sizing", exc_info=True
        )
        return {}


async def cancel_excess_protection(
    broker: BrokerPort,
    ledger: PositionLedger,
    audit: AuditPort | None = None,
    *,
    open_orders: list[OrderRecord],
    held_at_broker: dict[str, Decimal],
    execution: ProtectionInstaller | None = None,
    report: ReconciliationReport | None = None,
) -> int:
    """Cancel resting protective SELLs beyond what the account actually holds.

    The loop above asks one question per position: is *the stop we recorded*
    present and correctly sized. It never asks the reverse question — does the
    venue hold SELL orders we do not know about — and that blind spot is where
    every protective failure ends up.

    Three unrelated bugs converge on the same observable state. A duplicate stop
    from two overlapping passes. A stop left oversized after the venue shrank the
    position. A stop orphaned above a position that was emergency-closed while
    the stop's own submit reply was in flight. In each case more shares are
    promised to a resting SELL than exist, and whichever order triggers first
    sells stock the account does not have — opening a short, in a system whose
    risk policy disables shorting.

    So rather than guard each cause, this enforces the consequence directly, and
    reads the venue's own position book to do it. Excess is cancelled newest
    first: the oldest resting stop is the one the ledger is most likely to have
    recorded, and keeping it means the book and the venue still agree afterwards.
    """
    rep = report if report is not None else ReconciliationReport()
    if not held_at_broker and not ledger.get_open():
        return 0

    cancelled = 0
    by_symbol: dict[str, list[OrderRecord]] = {}
    for order in open_orders:
        if order.side is not OrderSide.SELL or not order.broker_order_id:
            continue
        if order.order_type not in {OrderType.STOP, OrderType.STOP_LIMIT}:
            continue
        by_symbol.setdefault(order.symbol.upper(), []).append(order)

    for symbol, orders in by_symbol.items():
        held = held_at_broker.get(symbol, Decimal(0))
        promised = sum((Decimal(str(o.qty)) for o in orders), Decimal(0))
        rep.checked.append(f"excess_protection:{symbol}")
        if promised <= held:
            continue

        if audit:
            await audit.append(
                "ExcessProtectionDetected",
                "reconcile",
                {
                    "symbol": symbol,
                    "held_qty": str(held),
                    "protected_qty": str(promised),
                    "orders": [o.broker_order_id for o in orders],
                    "severity": SEVERITY_CRITICAL,
                },
                entity_type="position",
                entity_id=symbol,
            )

        if execution is None:
            rep.unresolved.append(f"excess_protection:{symbol}:{promised}>{held}")
            continue

        # Newest first, so the surviving stop is the one the ledger points at.
        for order in sorted(orders, key=_order_recency, reverse=True):
            if promised <= held:
                break
            await execution.cancel_protection(
                broker_order_id=order.broker_order_id or "",
                symbol=symbol,
                reason=f"excess protection: {promised} promised against {held} held",
            )
            promised -= Decimal(str(order.qty))
            cancelled += 1
            rep.changed.append(f"excess_protection:{symbol}:cancelled")

        if promised > held:
            rep.unresolved.append(f"excess_protection:{symbol}:{promised}>{held}")

    return cancelled


def _order_recency(order: OrderRecord) -> Any:
    """Sort key for "most recently created". Missing timestamps sort oldest."""
    return getattr(order, "submitted_at", None) or getattr(order, "created_at", None) or ""


async def _report_protection_unverified(
    ledger: PositionLedger,
    audit: AuditPort | None,
    rep: ReconciliationReport,
) -> None:
    """Record that protection could not be checked, position by position."""
    for row in ledger.get_open():
        rep.unresolved.append(f"protection:{row.symbol}:unverified")
        if audit:
            await audit.append(
                "ProtectionUnverified",
                "reconcile",
                {
                    "symbol": row.symbol,
                    "position_id": str(row.id),
                    "qty": str(row.qty),
                    "expected_stop_order_id": (row.payload or {}).get("stop_order_id"),
                    "reason": "broker open orders unreadable",
                    "severity": SEVERITY_CRITICAL,
                },
                entity_type="position",
                entity_id=str(row.id),
            )


async def _resize_stale_protection(
    ledger: PositionLedger,
    audit: AuditPort | None,
    execution: ProtectionInstaller | None,
    row: Any,
    *,
    stop: OrderRecord,
    held: Decimal,
    rep: ReconciliationReport,
) -> None:
    if audit:
        await audit.append(
            "ProtectionQuantityMismatch",
            "reconcile",
            {
                "symbol": row.symbol,
                "position_id": str(row.id),
                "position_qty": str(held),
                "stop_qty": str(stop.qty),
                "stop_order_id": stop.broker_order_id,
                "severity": SEVERITY_CRITICAL if stop.qty > held else SEVERITY_WARNING,
            },
            entity_type="position",
            entity_id=str(row.id),
        )
    if execution is None:
        rep.unresolved.append(f"protection:{row.symbol}:qty_mismatch")
        return

    new_oid = await execution.resize_protection(
        symbol=row.symbol,
        position_id=row.id,
        remaining_qty=held,
        stop_price=Decimal(str(row.stop_price)) if row.stop_price is not None else None,
        reason="reconcile: resting stop does not match position size",
        previous_stop_order_id=stop.broker_order_id,
    )
    if new_oid is None:
        rep.unresolved.append(f"protection:{row.symbol}:resize_failed")
        return
    ledger.set_stop_order_id(row.id, new_oid)
    rep.changed.append(f"protection:{row.symbol}:resized")


def _is_traido_entry_client_id(client_order_id: str) -> bool:
    cid = (client_order_id or "").lower()
    return cid.startswith(("traido-e-", "traido-entry-"))


def _any_approving() -> bool:
    from trading.opportunities import OPPORTUNITIES

    store = OPPORTUNITIES
    if hasattr(store, "has_status"):
        return bool(store.has_status(OpportunityStatus.APPROVING))
    if hasattr(store, "_items"):
        return any(o.status == OpportunityStatus.APPROVING for o in store._items.values())
    return False


async def cancel_orphaned_entry_orders(
    broker: BrokerPort,
    audit: AuditPort | None = None,
    *,
    execution: ProtectionInstaller | None = None,
) -> int:
    """
    After FILL_TIMEOUT we release the card but cancel can fail silently —
    leftover accepted limits then sit on Alpaca with 0 fill. Sweep them when
    no BUY is mid-flight (APPROVING).

    Cancelling goes through the execution service like placing does. This was
    the one broker mutation reconciliation performed itself, and the static
    guard that keeps `place_order` inside the execution layer did not cover
    `cancel_order` — so the exception was invisible as well as real. Cancelling
    a live order is not a smaller act than placing one: it is what removes a
    stop, and it needs the same audit trail and the same single path out.
    """
    if _any_approving():
        return 0

    canceled = 0
    try:
        open_orders = await broker.list_open_orders()
    except Exception:  # noqa: BLE001
        return 0

    for order in open_orders:
        if order.side != OrderSide.BUY:
            continue
        if not _is_traido_entry_client_id(order.client_order_id):
            continue
        if order.filled_qty and order.filled_qty > 0:
            continue
        if not order.broker_order_id:
            continue
        if execution is None:
            # Without a service there is no sanctioned way to cancel. Reported
            # rather than done directly: a sweep that reaches past the execution
            # layer when its dependency is missing is how the exception becomes
            # permanent.
            logger.warning(
                "reconcile: stray entry order %s left in place, no execution service",
                order.broker_order_id,
            )
            continue
        try:
            await execution.cancel_entry_order(
                broker_order_id=order.broker_order_id,
                symbol=order.symbol,
                reason="orphaned entry order after fill timeout",
            )
            canceled += 1
            if audit:
                await audit.append(
                    "OrphanEntryOrderCanceled",
                    "reconcile",
                    {
                        "symbol": order.symbol,
                        "broker_order_id": order.broker_order_id,
                        "client_order_id": order.client_order_id,
                        "limit_price": str(order.limit_price) if order.limit_price else None,
                    },
                    entity_type="order",
                    entity_id=order.broker_order_id,
                )
        except Exception:
            # One un-cancellable order must not stop us cleaning up the rest.
            logger.warning(
                "reconcile: failed to cancel stray order %s", order.broker_order_id, exc_info=True
            )
            continue
    return canceled
