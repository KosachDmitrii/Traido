"""Supervisor — thin wrapper over the professional trader desk chain.

Never places orders. Capital path still goes: candidate → RiskEngine → desk card.
"""

from __future__ import annotations

from agents.trader.orchestrator import run_trader_desk
from core.activity import BOARD
from core.audit import create_audit
from core.config import Settings, get_settings
from core.enums import Timeframe
from core.ports import AuditPort, MarketDataPort
from core.schemas import PipelineResult
from market_data.factory import create_market_data_port

DEFAULT_TIMEFRAMES = (Timeframe.D1, Timeframe.H1, Timeframe.M15)


class Supervisor:
    def __init__(
        self,
        *,
        market_data: MarketDataPort,
        audit: AuditPort,
        settings: Settings | None = None,
    ) -> None:
        self.market_data = market_data
        self.audit = audit
        self.settings = settings or get_settings()

    async def scan_symbol(
        self,
        symbol: str,
        *,
        timeframes: tuple[Timeframe, ...] = DEFAULT_TIMEFRAMES,
        lookback_days: int = 400,
    ) -> PipelineResult:
        _ = timeframes, lookback_days  # desk loads D1/H1 itself from Alpaca
        BOARD.set_agent("scanner", status="working", detail="Trader desk", symbol=symbol.upper())
        BOARD.log("scanner", f"Trader desk analysis of {symbol.upper()}", symbol=symbol.upper())
        return await run_trader_desk(
            symbol,
            market_data=self.market_data,
            audit=self.audit,
            settings=self.settings,
        )


def build_supervisor(
    settings: Settings | None = None,
    *,
    market_data: MarketDataPort | None = None,
) -> Supervisor:
    settings = settings or get_settings()
    return Supervisor(
        market_data=market_data or create_market_data_port(settings),
        audit=create_audit(),
        settings=settings,
    )
