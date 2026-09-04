"""An earnings calendar nobody read is an unverified risk, not an absent one.

pytestmark = pytest.mark.usefixtures("capital_path_ready")

A swing stop is worthless across a print: the gap opens past it, and the loss is
whatever the tape decides overnight. The engine's answer is to refuse the days
around a print — which is worth exactly nothing when the calendar behind it was
never fetched. Before this, no Finnhub key meant two None dates, and two None
dates read identically to "the calendar was checked and is clear", so every
proposal cleared an event-risk gate that had never run.

The shape of the fix is borrowed from the liquidity gate, which had the same
problem with quotes: label where the number came from, and let the policy decide
whether an unverifiable one may pass.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from core.audit import InMemoryAudit
from core.enums import (
    EarningsCheck,
    NewsCheck,
    OpportunityStatus,
    RiskVerdict,
    SectorCheck,
    TradeAction,
    TradingMode,
    UserDecision,
)
from core.schemas import PortfolioSnapshot, RiskLimits, TradeCandidate
from market_data.providers.earnings import EarningsCalendar, parse_earnings_payload
from risk.kill_switch import set_kill_switch
from risk.risk_engine import RiskContext, RiskEngine
from tests.support import CLEARED_EARNINGS, liquid_market_data
from trading.execution import ExecutionService
from trading.exits import MemoryExitStore
from trading.opportunities import MemoryOpportunityStore

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("capital_path_ready")]

_NOW = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)


def _candidate(symbol: str = "AAPL") -> TradeCandidate:
    return TradeCandidate(
        symbol=symbol,
        action=TradeAction.BUY,
        entry=Decimal(100),
        stop=Decimal(95),
        target=Decimal(115),
        confidence=0.8,
        risk_reward=3.0,
        reasons=["earnings gate test"],
        strategy_version="test@1",
        pipeline_run_id=uuid4(),
    )


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
        equity=Decimal(100000),
        cash=Decimal(100000),
        buying_power=Decimal(100000),
        open_exposure=Decimal(0),
        open_positions=0,
        day_pnl=Decimal(0),
        week_pnl=Decimal(0),
        drawdown_pct=0.0,
        kill_switch=False,
    )


# ── The gate ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (EarningsCheck.NOT_CONFIGURED, "EARNINGS_CALENDAR_NOT_CONFIGURED"),
        (EarningsCheck.UNAVAILABLE, "EARNINGS_CALENDAR_UNAVAILABLE"),
        (EarningsCheck.NOT_CHECKED, "EARNINGS_UNVERIFIED"),
    ],
)
def test_an_unread_calendar_refuses_the_entry_and_names_the_cause(
    status: EarningsCheck, reason: str
) -> None:
    """The three causes stay apart because they need different responses.

    A missing key is an operator fixing config; an outage resolves itself. Behind
    one shared code they would be one undifferentiated count on the funnel, and
    the desk could not tell a five-minute fix from a vendor having a bad day.
    """
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=status,
        sector_check=SectorCheck.CHECKED,
        sector="technology",
        regime_tradable=True,
        now=_NOW,
    )
    decision = RiskEngine().evaluate(_candidate(), _portfolio(), context=ctx)

    assert decision.verdict is RiskVerdict.REJECT
    assert reason in decision.reasons


def test_a_calendar_that_answered_with_no_print_is_not_the_same_as_no_calendar() -> None:
    """The distinction the whole change exists to make.

    Both arrive at the engine as two None dates. One of them is a cleared check.
    """
    checked = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED,
        sector="technology",
        regime_tradable=True,
        now=_NOW,
    )
    unchecked = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.NOT_CONFIGURED,
        sector_check=SectorCheck.CHECKED,
        sector="technology",
        regime_tradable=True,
        now=_NOW,
    )
    engine = RiskEngine()

    assert engine.evaluate(_candidate(), _portfolio(), context=checked).verdict is RiskVerdict.PASS
    assert (
        engine.evaluate(_candidate(), _portfolio(), context=unchecked).verdict is RiskVerdict.REJECT
    )


def test_a_read_calendar_still_blocks_a_print_inside_the_window() -> None:
    """The requirement must not have replaced the check it protects."""
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector_check=SectorCheck.CHECKED,
        sector="technology",
        next_earnings=_NOW.date() + timedelta(days=1),
        regime_tradable=True,
        now=_NOW,
    )
    decision = RiskEngine().evaluate(_candidate(), _portfolio(), context=ctx)

    assert "EARNINGS_IMMINENT" in decision.reasons
    assert "EARNINGS_UNVERIFIED" not in decision.reasons


# ── The escape hatch, and its receipt ────────────────────────────────────────


def test_running_without_a_calendar_is_possible_but_recorded() -> None:
    """Backtests and research need this. So the trade has to carry the receipt.

    `limits_applied` is persisted with the decision, so afterwards it is
    answerable which trades were taken with event risk unchecked — rather than
    the question being unanswerable because nothing distinguished them.
    """
    limits = RiskLimits(require_earnings_check=False)
    ctx = RiskContext(
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.NOT_CONFIGURED,
        sector_check=SectorCheck.CHECKED,
        sector="technology",
        regime_tradable=True,
        now=_NOW,
    )
    decision = RiskEngine(limits).evaluate(_candidate(), _portfolio(), context=ctx)

    assert decision.verdict is RiskVerdict.PASS
    assert decision.limits_applied.require_earnings_check is False
    assert decision.earnings_check is EarningsCheck.NOT_CONFIGURED


def test_the_strict_default_survives_a_config_that_says_nothing_about_it() -> None:
    """The locked config predates the field. Silence must not mean "off"."""
    assert RiskLimits().require_earnings_check is True


# ── Carried to the click ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_proposal_that_cleared_the_calendar_is_not_refused_at_the_click() -> None:
    """Approval re-runs risk, and used to re-run it against an empty context.

    That re-check is there to catch what moved while the card waited — the book,
    the kill switch. Losing the calendar the proposal had already read would
    refuse a sound trade, and refuse it with the reason code for a missing
    vendor key, which sends the operator to configuration for a problem that is
    not there.
    """
    from broker.paper.mock import MockPaperBroker

    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    assert risk.earnings_check is EarningsCheck.CHECKED

    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    service = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        market_data=liquid_market_data(),
    )

    result = await service.decide(
        opp.id,
        UserDecision.APPROVE,
        request_id=uuid4(),
        expected_decision_version=opp.decision_version,
    )
    assert result.status == OpportunityStatus.EXECUTED


@pytest.mark.asyncio
@pytest.mark.usefixtures("keyless_earnings_calendar")
async def test_a_calendar_that_cannot_be_read_at_the_click_stops_the_order() -> None:
    """The re-check re-derives; it does not trust the card in front of it.

    Approval is the last gate before capital moves, and the card was sized up to
    an hour ago. A proposal sized as though it had passed must still be refused
    here when the calendar cannot be read now — otherwise the requirement is
    walked past by clicking rather than by scanning.
    """
    from broker.paper.mock import MockPaperBroker

    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine(RiskLimits(require_earnings_check=False)).evaluate(
        _candidate(),
        await broker.get_portfolio(),
        context=RiskContext(
            news=NewsCheck.CHECKED,
            earnings=EarningsCheck.NOT_CONFIGURED,
            sector_check=SectorCheck.CHECKED,
            sector="technology",
            regime_tradable=True,
        ),
    )
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)
    service = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        market_data=liquid_market_data(),
    )

    with pytest.raises(RuntimeError, match="EARNINGS_CALENDAR_NOT_CONFIGURED"):
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    assert broker.orders == []


@pytest.mark.asyncio
async def test_a_print_that_appears_while_the_card_waits_stops_the_approval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Why the re-check re-derives instead of trusting the card.

    A proposal lives up to an hour. If the calendar it cleared says something
    different by the time the button is pressed, the button is what matters —
    inheriting the earlier answer would put the position on precisely through
    the event the whole check exists to avoid.
    """
    from broker.paper.mock import MockPaperBroker
    from market_data.providers.earnings import EarningsInfo

    class _PrintTomorrow:
        async def get(self, symbol: str, *, now: datetime | None = None) -> EarningsInfo:
            today = (now or _NOW).date()
            return EarningsInfo(
                symbol=symbol.upper(),
                next_date=today + timedelta(days=1),
                status=EarningsCheck.CHECKED,
            )

    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    # Proposed against a clear calendar, the way the scan would have found it.
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    assert risk.verdict is RiskVerdict.PASS
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)

    monkeypatch.setattr("risk.context_builder.get_earnings_calendar", lambda _key: _PrintTomorrow())
    service = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        market_data=liquid_market_data(),
    )

    with pytest.raises(RuntimeError, match="EARNINGS_IMMINENT"):
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    assert broker.orders == []


@pytest.mark.asyncio
async def test_a_context_that_cannot_be_built_refuses_rather_than_waves_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rebuild is a check, so its failure is a rejection.

    Were it swallowed, the strictest re-check in the system would degrade into
    no check at all under exactly the conditions — a vendor down, a broker
    unreachable — that make trading least safe.
    """
    from broker.paper.mock import MockPaperBroker

    async def _explode(*_args, **_kwargs):
        raise RuntimeError("vendor down")

    set_kill_switch(False)
    broker = MockPaperBroker()
    store = MemoryOpportunityStore()
    risk = RiskEngine().evaluate(
        _candidate(), await broker.get_portfolio(), context=CLEARED_EARNINGS
    )
    opp = store.create(_candidate(), risk, TradingMode.CONFIRMATION)

    monkeypatch.setattr("trading.execution.build_risk_context", _explode)
    service = ExecutionService(
        broker=broker,
        audit=InMemoryAudit(),
        store=store,
        exit_store=MemoryExitStore(),
        market_data=liquid_market_data(),
    )

    with pytest.raises(RuntimeError, match="RISK_REJECT"):
        await service.decide(
            opp.id,
            UserDecision.APPROVE,
            request_id=uuid4(),
            expected_decision_version=opp.decision_version,
        )
    assert broker.orders == []


# ── What the provider reports ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_api_key_reports_not_configured_rather_than_a_clear_calendar() -> None:
    info = await EarningsCalendar(api_key=None).get("AAPL", now=_NOW)

    assert info.status is EarningsCheck.NOT_CONFIGURED
    assert info.available is False
    assert info.next_date is None


@pytest.mark.asyncio
async def test_a_missing_key_is_explained_once_but_enforced_every_time() -> None:
    """One condition affects the whole universe; sixty warnings say it sixty times.

    The prose is what floods the log. The status is what the engine acts on, so
    it has to survive every call — quieting the note must not quiet the gate.
    """
    calendar = EarningsCalendar(api_key=None)

    first = await calendar.get("AAPL", now=_NOW)
    rest = [await calendar.get(s, now=_NOW) for s in ("MSFT", "NVDA", "KO")]

    assert first.note
    assert [i.note for i in rest] == ["", "", ""]
    assert all(i.status is EarningsCheck.NOT_CONFIGURED for i in [first, *rest])


def test_an_empty_window_from_the_vendor_is_a_cleared_check() -> None:
    """Finnhub answering "nothing scheduled" is real information, not a gap."""
    info = parse_earnings_payload("AAPL", {"earningsCalendar": []}, date(2026, 3, 10))

    assert info.status is EarningsCheck.CHECKED
    assert info.available is True


def test_a_malformed_payload_is_unavailable_not_clear() -> None:
    info = parse_earnings_payload("AAPL", {"unexpected": 1}, date(2026, 3, 10))

    assert info.status is EarningsCheck.UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.usefixtures("keyless_earnings_calendar")
async def test_the_context_builder_reports_what_the_calendar_actually_said() -> None:
    """The seam where the whole gate could be quietly disconnected.

    The provider can label a missing key perfectly and the engine can refuse an
    unlabelled context perfectly, and the two together still do nothing if the
    status is dropped between them. Nothing else in the suite crosses this join.
    """
    from broker.paper.mock import MockPaperBroker
    from risk.context_builder import build_risk_context

    class _NoBars:
        async def get_bars(self, *_args, **_kwargs):  # pragma: no cover - unused
            return []

    result = await build_risk_context(
        "AAPL",
        broker=MockPaperBroker(),
        market_data=_NoBars(),  # type: ignore[arg-type]
        finnhub_api_key=None,
        now=_NOW,
    )

    assert result.context.earnings is EarningsCheck.NOT_CONFIGURED
    assert any("Finnhub key not configured" in n for n in result.notes)
