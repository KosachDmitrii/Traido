"""Closed entry linkage, broker flatness and fresh-pattern reentry; no order side effects."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.enums import OpportunityStatus, TradingMode
from database.models.journal import TradeJournalRow
from database.models.positions import OpenPositionRow
from database.session import session_factory
from strategy.orb.publication import publish_orb
from strategy.orb.reentry import rearm_closed_trade
from strategy.orb.store import read_session
from tests.conftest import RTH_INSTANT
from tests.unit.test_orb_publication import proposed
from trading.auto_trigger_policy import orb_execution_statuses
from trading.opportunities import OpportunityStore


def closed_trade(*, closed=True, journal=True):
    result, final = proposed()
    opp = publish_orb(result, final, TradingMode.CONFIRMATION, now=RTH_INSTANT)
    OpportunityStore().claim(
        opp.id,
        from_status=OpportunityStatus.AWAITING_CONFIRMATION,
        to_status=OpportunityStatus.EXECUTED,
    )
    pid = uuid4()
    with session_factory()() as db:
        db.add(
            OpenPositionRow(
                id=pid,
                opportunity_id=opp.id,
                symbol=result.symbol,
                qty=Decimal(0 if closed else 1),
                avg_entry=result.candidate.entry,
                strategy_version=result.candidate.strategy_version,
                status="closed" if closed else "open",
                opened_at=RTH_INSTANT - timedelta(minutes=30),
                closed_at=RTH_INSTANT.astimezone(UTC) if closed else None,
                payload={},
                entry_reasons=[],
            )
        )
        if journal:
            db.add(
                TradeJournalRow(
                    position_id=pid,
                    symbol=result.symbol,
                    entry=result.candidate.entry,
                    exit=result.candidate.entry,
                    qty=1,
                    pnl=0,
                    pnl_pct=0,
                    strategy_version=result.candidate.strategy_version,
                    closed_at=RTH_INSTANT.astimezone(UTC),
                )
            )
        db.commit()
    return opp, result.candidate.orb_plan["session"]


class FlatBroker:
    def __init__(self, positions=(), orders=(), error=False):
        self.positions, self.orders, self.error = positions, orders, error

    async def list_positions(self):
        if self.error:
            raise RuntimeError("unavailable")
        return self.positions

    async def list_open_orders(self):
        return self.orders


@pytest.mark.asyncio
async def test_closed_trade_rearms_new_plan_once_without_rewriting_old_execution():
    opp, day = closed_trade()
    before = OpportunityStore().get(opp.id).model_dump()
    assert (
        orb_execution_statuses(read_session(day)["states"])[opp.candidate.symbol]["stage"]
        == "CLOSED"
    )
    now = RTH_INSTANT + timedelta(minutes=1)
    assert await rearm_closed_trade(day, opp.candidate.symbol, FlatBroker(), now=now)
    saved = read_session(day)
    state = saved["states"][opp.candidate.symbol]
    assert "opportunity_id" not in state
    assert state["closed_opportunity_ids"] == [str(opp.id)]
    assert datetime.fromisoformat(state["retest_after"]) == RTH_INSTANT
    assert saved["plans"][opp.candidate.symbol]["version"] == "orb@2.0.0"
    assert not saved["plans"][opp.candidate.symbol]["evidence"].get("retest")
    assert not await rearm_closed_trade(day, opp.candidate.symbol, FlatBroker(), now=now)
    assert OpportunityStore().get(opp.id).model_dump() == before
    assert orb_execution_statuses(saved["states"]) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "blocker",
    ["local_open", "missing_journal", "broker_long", "broker_short", "resting_order", "unknown"],
)
async def test_reentry_keeps_old_claim_when_close_is_not_safe(blocker, monkeypatch):
    opp, day = closed_trade(closed=blocker != "local_open", journal=blocker != "missing_journal")
    symbol = opp.candidate.symbol
    broker = FlatBroker()
    if blocker in {"broker_long", "broker_short"}:
        broker.positions = [
            SimpleNamespace(symbol=symbol, qty=1 if blocker == "broker_long" else -1)
        ]
    if blocker == "resting_order":
        broker.orders = [SimpleNamespace(symbol=symbol)]
    if blocker == "unknown":
        from trading.intents import INTENTS

        monkeypatch.setattr(INTENTS, "unresolved_symbols", lambda: {symbol})
    original = read_session(day)
    assert not await rearm_closed_trade(day, symbol, broker, now=RTH_INSTANT + timedelta(minutes=1))
    assert read_session(day) == original
    if blocker == "missing_journal":
        assert orb_execution_statuses(original["states"])[symbol]["stage"] != "CLOSED"


@pytest.mark.asyncio
async def test_broker_failure_never_releases_closed_claim():
    opp, day = closed_trade()
    original = read_session(day)
    with pytest.raises(RuntimeError):
        await rearm_closed_trade(
            day,
            opp.candidate.symbol,
            FlatBroker(error=True),
            now=RTH_INSTANT + timedelta(minutes=1),
        )
    assert read_session(day) == original


def test_new_pattern_cannot_use_a_bar_started_before_close():
    from strategy.orb.retest import rebuild
    from tests.unit.test_orb_retest import scenario

    base, rows, now = scenario()
    after = rows[0].ts + timedelta(seconds=1)
    result = rebuild(base, rows, now=now, after=after)
    assert not result.plan.evidence.get("retest")


@pytest.mark.asyncio
@pytest.mark.usefixtures("capital_path_ready")
async def test_runtime_new_signal_after_close_publishes_new_opportunity(monkeypatch):
    from broker.paper.mock import MockPaperBroker
    from core.config import get_settings
    from database.session import get_sync_engine
    from strategy.orb import publication, runtime
    from strategy.orb.retest_data import _cache
    from tests.unit.test_orb_retest_execution import RetestMarket, current_plan
    from trading.opportunities import OPPORTUNITIES
    from trading.scan_context import ScanContext

    plan, rows = current_plan()
    clock = [RTH_INSTANT]

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz else clock[0].replace(tzinfo=None)

    monkeypatch.setattr(runtime, "datetime", Frozen)
    monkeypatch.setattr(publication, "datetime", Frozen)
    monkeypatch.setattr(OPPORTUNITIES, "_engine", get_sync_engine())
    monkeypatch.setattr(
        "trading.auto_trigger_policy.enqueue_auto_approve_opportunity", lambda *a, **k: None
    )
    _cache.clear()
    market = RetestMarket(plan, rows)
    market._now = lambda: clock[0]
    ctx = ScanContext(settings=get_settings(), broker=MockPaperBroker(), market_data=market)
    first = await runtime.evaluate_symbol(plan.symbol, ctx)
    assert first.opportunity is not None
    old_id = first.opportunity.id
    OPPORTUNITIES.claim(
        old_id,
        from_status=OpportunityStatus.AWAITING_CONFIRMATION,
        to_status=OpportunityStatus.EXECUTED,
    )
    pid = uuid4()
    with session_factory()() as db:
        db.add(
            OpenPositionRow(
                id=pid,
                opportunity_id=old_id,
                symbol=plan.symbol,
                qty=0,
                avg_entry=plan.max_entry,
                strategy_version=plan.version,
                status="closed",
                opened_at=RTH_INSTANT.astimezone(UTC),
                closed_at=RTH_INSTANT.astimezone(UTC),
            )
        )
        db.add(
            TradeJournalRow(
                position_id=pid,
                symbol=plan.symbol,
                entry=plan.max_entry,
                exit=plan.max_entry,
                qty=1,
                pnl=0,
                pnl_pct=0,
                strategy_version=plan.version,
                closed_at=RTH_INSTANT.astimezone(UTC),
            )
        )
        db.commit()
    clock[0] += timedelta(seconds=1)
    waiting = await runtime.evaluate_symbol(plan.symbol, ctx)
    assert waiting.opportunity is None  # original confirmation cannot be recycled
    market.rows = rows + [
        b.model_copy(update={"ts": b.ts + timedelta(minutes=15)}) for b in rows[-3:]
    ]
    clock[0] = RTH_INSTANT + timedelta(minutes=15)
    second = await runtime.evaluate_symbol(plan.symbol, ctx)
    assert second.opportunity is not None, (second.status, second.errors)
    assert second.opportunity.id != old_id
    assert (
        second.opportunity.creation_admission_record_id
        != first.opportunity.creation_admission_record_id
    )
    assert second.candidate.orb_plan["evidence"]["reentry"]["previous_opportunity_id"] == str(
        old_id
    )
    assert OPPORTUNITIES.get(old_id).status == OpportunityStatus.EXECUTED
    again = await runtime.evaluate_symbol(plan.symbol, ctx)
    assert again.opportunity.id == second.opportunity.id
    _cache.clear()
