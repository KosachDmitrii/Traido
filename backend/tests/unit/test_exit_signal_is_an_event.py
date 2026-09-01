"""The position agent may only sell on something that happened.

On 2026-08-31 the desk filled MO at 68.28 and proposed selling it eighteen
seconds later, citing "SMA20 crossed below EMA50 while in profit". Nothing had
crossed. Three separate defects combined to produce that card, and each one is
pinned here:

* the rule compared two levels on the newest bar and called the result a cross,
  so a gap that had been open for weeks re-fired on every pass;
* "in profit" meant a single tick, less than the spread paid to open the
  position and paid again to close it;
* the entry was drawn on the intraday series and the exit judged the daily one,
  so the condition was already true at the moment of purchase.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from agents.position.agent import MIN_EXIT_PROFIT_PCT, assess_exits
from core.enums import PositionStatus, Timeframe
from core.schemas import Bar, Position
from trading.exits import EXIT_APPROVING, EXIT_AWAITING, EXITS

# Sixty flat bars put SMA20 exactly on EMA50; one hard down bar pulls the
# faster average through the slower one. SMA20 lands at 98.00 against an EMA50
# of 98.43 — a cross, on that bar and no other.
FLAT = 100.0
CRASH = 60.0
CROSS_SERIES = [FLAT] * 60 + [CRASH]
AFTER_CROSS_SERIES = [FLAT] * 60 + [CRASH, CRASH]


class FakeBroker:
    def __init__(self, position: Position) -> None:
        self._position = position

    async def list_positions(self) -> list[Position]:
        return [self._position]


class SeriesMarketData:
    """Serves one designed close series and records what was asked for."""

    def __init__(self, closes: list[float]) -> None:
        self.closes = closes
        self.requested: list[Timeframe] = []

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        self.requested.append(timeframe)
        base = datetime.now(UTC) - timedelta(days=len(self.closes))
        return [
            Bar(
                symbol=symbol,
                timeframe=timeframe,
                ts=base + timedelta(days=i),
                open=close,
                high=close * 1.001,
                low=close * 0.999,
                close=close,
                volume=5_000_000.0,
                source="synthetic",
            )
            for i, close in enumerate(self.closes)
        ]


def _position(entry: float) -> Position:
    """A position with no stop or target, so only the cross rule can speak."""
    return Position(
        id=uuid.uuid4(),
        symbol="TEST",
        qty=Decimal(10),
        avg_entry=Decimal(str(entry)),
        stop_price=None,
        target_price=None,
        status=PositionStatus.OPEN,
        opened_at=datetime.now(UTC),
    )


@pytest.fixture(autouse=True)
def _no_ledger_row(monkeypatch):
    """Most cases here are about the rule, not about ledger plumbing."""
    monkeypatch.setattr("agents.position.agent.LEDGER.find_open_by_symbol", lambda symbol: None)


@pytest.mark.asyncio
async def test_a_cross_is_proposed() -> None:
    """The bar where the averages actually cross does produce a sell."""
    market = SeriesMarketData(CROSS_SERIES)
    # Entry well below the close, so the profit clears the round-trip cost.
    out = await assess_exits(FakeBroker(_position(50.0)), market)

    assert len(out) == 1
    assert any("crossed below" in r for r in out[0].proposal.reasons)


@pytest.mark.asyncio
async def test_a_gap_that_was_already_open_is_not_a_cross() -> None:
    """The MO card: SMA20 below EMA50 on this bar *and* the one before it.

    Identical data to the passing case, one bar later. The averages are still
    apart and still in the same order — nothing crossed, so nothing is proposed.
    The replaced rule fired here, and would have gone on firing every pass for
    as long as the gap stayed open.
    """
    market = SeriesMarketData(AFTER_CROSS_SERIES)
    out = await assess_exits(FakeBroker(_position(50.0)), market)

    assert out == []


def _entry_for_gain(pct: float) -> float:
    """The entry price a close of `CRASH` would represent `pct` profit on."""
    return CRASH / (1 + pct / 100)


def test_the_profit_floor_covers_both_crossings() -> None:
    """Stated as a number, not derived, so zeroing the constant is visible.

    Ten basis points to cross on the way in and ten on the way out. The two
    cases below straddle it with fixed percentages; if this relationship stops
    holding, they stop testing anything.
    """
    assert 0.1 < MIN_EXIT_PROFIT_PCT < 1.0


@pytest.mark.asyncio
async def test_a_profit_smaller_than_the_round_trip_is_not_a_profit() -> None:
    """A tenth of a percent: a gain that closing the position would erase."""
    market = SeriesMarketData(CROSS_SERIES)

    out = await assess_exits(FakeBroker(_position(_entry_for_gain(0.1))), market)

    assert out == []


@pytest.mark.asyncio
async def test_a_profit_that_clears_the_round_trip_is_proposed() -> None:
    """The same cross, on a gain that survives being closed."""
    market = SeriesMarketData(CROSS_SERIES)

    out = await assess_exits(FakeBroker(_position(_entry_for_gain(1.0))), market)

    assert len(out) == 1


@pytest.mark.asyncio
async def test_the_exit_reads_the_series_the_entry_was_drawn_on(monkeypatch) -> None:
    """An H1 entry is judged on H1, not on the daily default."""
    monkeypatch.setattr(
        "agents.position.agent.LEDGER.find_open_by_symbol",
        lambda symbol: SimpleNamespace(
            id=uuid.uuid4(),
            stop_price=None,
            target_price=None,
            opened_at=datetime.now(UTC),
            payload={"exec_timeframe": Timeframe.H1.value},
        ),
    )
    market = SeriesMarketData(CROSS_SERIES)

    await assess_exits(FakeBroker(_position(50.0)), market)

    assert market.requested == [Timeframe.H1]


@pytest.mark.asyncio
async def test_a_position_without_a_recorded_timeframe_falls_back_to_daily() -> None:
    """Positions opened before the timeframe was recorded still get watched."""
    market = SeriesMarketData(CROSS_SERIES)

    await assess_exits(FakeBroker(_position(50.0)), market)

    assert market.requested == [Timeframe.D1]


@pytest.mark.asyncio
async def test_a_card_whose_reason_expired_is_withdrawn() -> None:
    """The desk stops offering a sell the rule no longer supports."""
    broker = FakeBroker(_position(50.0))
    await assess_exits(broker, SeriesMarketData(CROSS_SERIES))
    assert [c.proposal.symbol for c in EXITS.list_open()] == ["TEST"]

    # One bar later the gap is merely open, not crossing.
    await assess_exits(broker, SeriesMarketData(AFTER_CROSS_SERIES))

    assert EXITS.list_open() == []


@pytest.mark.asyncio
async def test_a_card_is_not_withdrawn_when_the_data_could_not_be_read() -> None:
    """Silence from the feed is not the rule changing its mind.

    Withdrawing here would let a vendor outage quietly clear the desk of
    warnings that were correct when they were raised.
    """

    class StaleMarketData(SeriesMarketData):
        async def get_bars(self, symbol, timeframe, start, end):
            bars = await super().get_bars(symbol, timeframe, start, end)
            return [b.model_copy(update={"ts": b.ts - timedelta(days=30)}) for b in bars]

    broker = FakeBroker(_position(50.0))
    await assess_exits(broker, SeriesMarketData(CROSS_SERIES))
    assert len(EXITS.list_open()) == 1

    await assess_exits(broker, StaleMarketData(CROSS_SERIES))

    assert len(EXITS.list_open()) == 1


@pytest.mark.asyncio
async def test_a_card_being_acted_on_is_not_withdrawn() -> None:
    """An operator who just pressed SELL keeps the card they pressed."""
    broker = FakeBroker(_position(50.0))
    await assess_exits(broker, SeriesMarketData(CROSS_SERIES))
    card = EXITS.list_open()[0]
    EXITS.claim(card.id, from_status=EXIT_AWAITING, to_status=EXIT_APPROVING)

    await assess_exits(broker, SeriesMarketData(AFTER_CROSS_SERIES))

    assert EXITS.get(card.id).status == EXIT_APPROVING


@pytest.mark.asyncio
async def test_stale_bars_produce_no_exit_signal() -> None:
    """A series that stopped updating cannot justify a sell.

    Silence is safe here in a way it would not be on the entry path: the
    protective stop is already resting at the broker and is untouched by the
    position agent declining to speak.
    """

    class StaleMarketData(SeriesMarketData):
        async def get_bars(self, symbol, timeframe, start, end):
            bars = await super().get_bars(symbol, timeframe, start, end)
            shift = timedelta(days=30)
            return [b.model_copy(update={"ts": b.ts - shift}) for b in bars]

    out = await assess_exits(FakeBroker(_position(50.0)), StaleMarketData(CROSS_SERIES))

    assert out == []
