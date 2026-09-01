"""Shared scaffolding for tests that need a trade to reach execution.

A `RiskContext` says nothing about the earnings calendar by default, and the
engine refuses an entry whose calendar was never read. That is the point of the
default — but a test about order lifecycle or idempotency is not a test about
event risk, and restating the calendar at every call site would bury what each
one is actually asserting. So it is stated once, here, by name.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from core.enums import EarningsCheck, NewsCheck, SectorCheck, Timeframe
from core.schemas import Bar, Quote
from risk.risk_engine import RiskContext

CLEARED_EARNINGS = RiskContext(
    earnings=EarningsCheck.CHECKED,
    news=NewsCheck.CHECKED,
    sector="technology",
    sector_check=SectorCheck.CHECKED,
)
"""Vendor checks and sector were established — what a live entry has cleared.

The calendar was read and no print is near; the headlines were read and nothing
in them vetoes; the name sits in a known sector. Tests about order lifecycle or
sizing are not tests about those gates, but they still have to clear all three
to reach their subject.
"""


class LiquidMarketData:
    """A symbol that comfortably clears the liquidity gate.

    The same reasoning as `CLEARED_EARNINGS`, for the other gate that refuses an
    entry it could not measure: a test about order lifecycle is not a test about
    spread, but it still has to get past the spread check to reach its subject.

    The quote is stamped on whatever clock the execution service is reading,
    which the suite freezes to a mid-session instant. Stamping it from the wall
    clock instead makes every entry test fail as `QUOTE_STALE` — correctly, and
    for a reason that has nothing to do with what the test is asserting.
    """

    def __init__(self, *, price: float = 100.0, volume: float = 5_000_000.0) -> None:
        self.price = price
        self.volume = volume

    @staticmethod
    def _now() -> datetime:
        from trading import execution

        return execution._utcnow()

    async def get_quote(self, symbol: str) -> Quote | None:
        half = Decimal(str(self.price)) * Decimal("0.0001")
        return Quote(
            symbol=symbol,
            bid=Decimal(str(self.price)) - half,
            ask=Decimal(str(self.price)) + half,
            ts=self._now(),
            source="synthetic",
        )

    async def get_bars(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Bar]:
        base = self._now() - timedelta(days=60)
        return [
            Bar(
                symbol=symbol,
                timeframe=Timeframe.D1,
                ts=base + timedelta(days=i),
                open=self.price,
                high=self.price * 1.01,
                low=self.price * 0.99,
                close=self.price,
                volume=self.volume,
                source="synthetic",
            )
            for i in range(60)
        ]

    async def get_last_price(self, symbol: str) -> float:
        return self.price


def liquid_market_data(**kwargs: float) -> LiquidMarketData:
    """A market-data port for tests whose subject is not liquidity."""
    return LiquidMarketData(**kwargs)  # type: ignore[arg-type]
