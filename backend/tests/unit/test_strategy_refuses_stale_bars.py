"""The series a decision is drawn from has an age limit too.

`check_bar_freshness` guards the daily bars behind liquidity. Nothing used to
guard the intraday series the desk prices from — and on 2026-08-31 that is
exactly where the staleness was: daily bars were current, hourly ones were
seven weeks behind (Alpaca's oldest page when `next_page_token` was ignored).

Trader desk loads H1 in universe. A stale H1 must fail the step — not be
dropped so setup/entry quietly substitute D1.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agents.trader.types import TraderBundle
from agents.trader.universe import run_universe
from core.enums import Timeframe
from core.schemas import Bar


def _series(
    newest: datetime,
    timeframe: Timeframe,
    *,
    symbol: str = "AAPL",
    count: int = 80,
    close: float = 100.0,
    volume: float = 500_000,
) -> list[Bar]:
    step = timedelta(days=1) if timeframe is Timeframe.D1 else timedelta(hours=1)
    return [
        Bar(
            symbol=symbol,
            timeframe=timeframe,
            ts=newest - step * (count - 1 - i),
            open=Decimal(str(close)),
            high=Decimal(str(close + 1)),
            low=Decimal(str(close - 1)),
            close=Decimal(str(close)),
            volume=Decimal(str(volume)),
            source="synthetic",
        )
        for i in range(count)
    ]


class _Feed:
    """Fresh daily bars, and an hourly series whose newest bar is controlled."""

    def __init__(self, *, hourly_newest: datetime, now: datetime) -> None:
        self.hourly_newest = hourly_newest
        self.now = now

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        if timeframe is Timeframe.D1:
            return _series(self.now, Timeframe.D1, symbol=symbol)
        return _series(self.hourly_newest, timeframe, symbol=symbol, count=60)


@pytest.mark.asyncio
async def test_an_hourly_series_weeks_behind_fails_the_scan() -> None:
    """The shape of the live defect: daily current, hourly seven weeks old."""
    now = datetime.now(UTC)
    bundle = TraderBundle(symbol="AAPL")
    step = await run_universe(
        bundle,
        _Feed(hourly_newest=now - timedelta(weeks=7), now=now),  # type: ignore[arg-type]
    )

    assert step.ok is False
    assert "STALE_BARS" in step.reasons
    assert Timeframe.H1 not in bundle.features, "stale H1 must not attach"


@pytest.mark.asyncio
async def test_a_current_hourly_series_is_not_refused() -> None:
    """The gate has to survive the ordinary case or it will be switched off."""
    now = datetime.now(UTC)
    bundle = TraderBundle(symbol="AAPL")
    step = await run_universe(
        bundle,
        _Feed(hourly_newest=now, now=now),  # type: ignore[arg-type]
    )

    assert step.ok is True, step.reasons
    assert Timeframe.H1 in bundle.features


@pytest.mark.asyncio
async def test_a_weekend_gap_is_not_staleness() -> None:
    """Friday's close to Tuesday's open is an ordinary gap, not a stopped feed."""
    now = datetime.now(UTC)
    bundle = TraderBundle(symbol="AAPL")
    step = await run_universe(
        bundle,
        _Feed(hourly_newest=now - timedelta(days=3), now=now),  # type: ignore[arg-type]
    )

    assert step.ok is True, step.reasons
