"""The series a decision is drawn from has an age limit too.

`check_bar_freshness` was wired into the entry gates, where it guards the daily
bars the liquidity verdict is computed from. Nothing guarded the intraday series
the strategy prices from — and on 2026-08-31 that is exactly where the staleness
was: the daily bars were correct all along, the hourly ones were seven weeks
behind, and the gate written for this failure had nothing to report because it
was looking at the wrong series.

A stale timeframe raises rather than being skipped. Skipping it silently changes
which timeframe the strategy takes its execution snapshot from, which is the
same quiet substitution in a different costume.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agents.supervisor.agent import Supervisor
from core.audit import InMemoryAudit
from core.config import Settings
from core.enums import Timeframe
from core.schemas import Bar

NOW = datetime(2026, 8, 31, 14, 0, tzinfo=UTC)


def _series(newest: datetime, timeframe: Timeframe, count: int = 60) -> list[Bar]:
    step = timedelta(days=1) if timeframe is Timeframe.D1 else timedelta(hours=1)
    return [
        Bar(
            symbol="AAPL",
            timeframe=timeframe,
            ts=newest - step * (count - 1 - i),
            open=Decimal(100),
            high=Decimal(101),
            low=Decimal(99),
            close=Decimal(100),
            volume=Decimal(1_000_000),
            source="synthetic",
        )
        for i in range(count)
    ]


class _Feed:
    """Fresh daily bars, and an hourly series stuck in the past."""

    def __init__(self, *, hourly_newest: datetime) -> None:
        self.hourly_newest = hourly_newest

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        if timeframe is Timeframe.D1:
            return _series(NOW, Timeframe.D1)
        return _series(self.hourly_newest, timeframe)

    async def get_quote(self, symbol: str) -> None:
        return None

    async def get_last_price(self, symbol: str) -> float:
        return 100.0


def _supervisor(feed: _Feed) -> Supervisor:
    return Supervisor(
        market_data=feed,  # type: ignore[arg-type]
        audit=InMemoryAudit(),
        settings=Settings(
            alpaca_api_key=None,
            alpaca_api_secret=None,
            finnhub_api_key=None,
            fred_api_key=None,
        ),
        clock=lambda: NOW,
    )


@pytest.mark.asyncio
async def test_an_hourly_series_weeks_behind_fails_the_scan() -> None:
    """The shape of the live defect: daily current, hourly seven weeks old."""
    supervisor = _supervisor(_Feed(hourly_newest=NOW - timedelta(weeks=7)))

    result = await supervisor.scan_symbol("AAPL", timeframes=(Timeframe.D1, Timeframe.H1))

    assert result.status == "failed"
    assert any("STALE_BARS" in e for e in result.errors)
    assert result.candidate is None, "a card was drawn from a series seven weeks old"


@pytest.mark.asyncio
async def test_a_current_hourly_series_is_not_refused() -> None:
    """The gate has to survive the ordinary case or it will be switched off."""
    supervisor = _supervisor(_Feed(hourly_newest=NOW))

    result = await supervisor.scan_symbol("AAPL", timeframes=(Timeframe.D1, Timeframe.H1))

    assert result.status != "failed", result.errors


@pytest.mark.asyncio
async def test_a_weekend_gap_is_not_staleness() -> None:
    """Friday's close to Tuesday's open is an ordinary gap, not a stopped feed."""
    supervisor = _supervisor(_Feed(hourly_newest=NOW - timedelta(days=3)))

    result = await supervisor.scan_symbol("AAPL", timeframes=(Timeframe.D1, Timeframe.H1))

    assert result.status != "failed", result.errors
