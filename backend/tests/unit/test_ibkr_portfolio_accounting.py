from __future__ import annotations

from decimal import Decimal

import pytest

from broker.ibkr import FakeIBKRTransport, IBKRBroker

pytestmark = pytest.mark.asyncio


class AccountSummaryTransport(FakeIBKRTransport):
    def __init__(self, summary: dict[str, str]) -> None:
        super().__init__()
        self.summary = summary

    async def account_summary(self) -> dict[str, str]:
        return self.summary


async def test_ibkr_explains_equity_above_cash_without_positions() -> None:
    broker = IBKRBroker(
        AccountSummaryTransport(
            {
                "NetLiquidation": "1000516",
                "TotalCashValue": "1000000",
                "BuyingPower": "4000000",
                "AccruedCash": "516",
                "GrossPositionValue": "0",
                "UnrealizedPnL": "0",
                "RealizedPnL": "0",
                "PreviousEquityWithLoanValue": "1000172",
                "BaseCurrency": "USD",
            }
        )
    )

    portfolio = await broker.get_portfolio()

    assert portfolio.equity == Decimal(1000516)
    assert portfolio.cash == Decimal(1000000)
    assert portfolio.non_cash_equity == Decimal(516)
    assert portfolio.accrued_cash == Decimal(516)
    assert portfolio.day_pnl == Decimal(344)
    assert portfolio.realized_pnl == Decimal(0)
    assert portfolio.day_pnl_source == "net_liquidation_vs_previous_equity"
    assert portfolio.base_currency == "USD"
    assert portfolio.week_pnl is None
    assert portfolio.drawdown_pct is None


async def test_ibkr_marks_realized_pnl_as_fallback_without_previous_equity() -> None:
    broker = IBKRBroker(
        AccountSummaryTransport(
            {
                "NetLiquidation": "1000100",
                "TotalCashValue": "1000000",
                "BuyingPower": "4000000",
                "RealizedPnL": "25",
            }
        )
    )

    portfolio = await broker.get_portfolio()

    assert portfolio.day_pnl == Decimal(25)
    assert portfolio.day_pnl_source == "realized_pnl_fallback"
