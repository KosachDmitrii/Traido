"""Offline fixture market data — deterministic Stage 1 development without API keys."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from core.enums import Timeframe
from core.schemas import Bar, Quote
from quant.aggregate import aggregate_bars

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "bars"


class FixtureMarketData:
    source = "fixture"

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or FIXTURES

    def _path(self, symbol: str, timeframe: Timeframe) -> Path:
        return self._root / f"{symbol.upper()}_{timeframe.value}.json"

    async def get_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Bar]:
        symbol = symbol.upper()
        if timeframe == Timeframe.H4:
            hourly = await self.get_bars(symbol, Timeframe.H1, start, end)
            return [
                b for b in aggregate_bars(hourly, Timeframe.H4, self.source) if start <= b.ts <= end
            ]

        path = self._path(symbol, timeframe)
        if not path.exists():
            raise FileNotFoundError(f"missing fixture {path}")
        rows = json.loads(path.read_text())
        out: list[Bar] = []
        for row in rows:
            ts = datetime.fromisoformat(row["ts"])
            if ts < start or ts > end:
                continue
            out.append(
                Bar(
                    symbol=symbol,
                    timeframe=timeframe,
                    ts=ts,
                    open=Decimal(str(row["open"])),
                    high=Decimal(str(row["high"])),
                    low=Decimal(str(row["low"])),
                    close=Decimal(str(row["close"])),
                    volume=Decimal(str(row["volume"])),
                    source=self.source,
                )
            )
        return out

    async def get_last_price(self, symbol: str) -> float:
        from datetime import timedelta

        end = datetime.now(UTC)
        start = end - timedelta(days=3650)
        bars = await self.get_bars(symbol, Timeframe.D1, start, end)
        if not bars:
            raise ValueError(f"no fixture bars for {symbol}")
        return float(bars[-1].close)

    async def get_quote(self, symbol: str) -> Quote | None:
        """Top of book from the last fixture close — arms the spread check offline.

        Without this, `build_execution_service` leaves `quotes=None` whenever
        Alpaca keys are absent, and the desk's liquidity gate cannot measure.
        A missing fixture symbol still returns None so the gate fails closed.
        """
        try:
            price = await self.get_last_price(symbol)
        except (FileNotFoundError, ValueError, OSError):
            return None
        px = Decimal(str(price))
        half = px * Decimal("0.0001")
        return Quote(
            symbol=symbol.upper(),
            bid=px - half,
            ask=px + half,
            ts=datetime.now(UTC),
            source=self.source,
        )
