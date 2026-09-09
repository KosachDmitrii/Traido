from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from api.routes import trading
from broker.ibkr import IBKRBroker
from database.models.risk_period import RiskPeriodRow
from database.session import session_factory
from risk.paper_period import observe, start_period
from tests.unit.test_ibkr_portfolio_accounting import AccountSummaryTransport

ACCOUNT = "DU12345"


@pytest.fixture
def broker(monkeypatch):
    transport = AccountSummaryTransport(
        {
            "Account": ACCOUNT,
            "BaseCurrency": "USD",
            "NetLiquidation": "100000",
            "TotalCashValue": "100000",
        }
    )
    instance = IBKRBroker(transport)
    instance.place_order = AsyncMock(side_effect=AssertionError("No broker mutations"))
    instance.cancel_order = AsyncMock(side_effect=AssertionError("No broker mutations"))
    monkeypatch.setattr(trading, "create_broker", lambda _: instance)
    monkeypatch.setattr("broker.switch_guard.broker_switch_blocked_reason", lambda: None)
    return instance


def body(account=ACCOUNT):
    return trading.PaperRiskStartBody(
        account_id=account, confirmation="START_NEW_OBSERVED_PAPER_PERIOD"
    )


@pytest.mark.asyncio
async def test_start_endpoint_and_retry_are_not_orders_or_reset(broker):
    result = await trading.start_risk_period(body())
    assert result.week_pnl == 0
    assert result.risk_period_id
    broker._transport.summary["NetLiquidation"] = "94000"
    result = await trading.start_risk_period(body())
    assert result.week_pnl == -6000
    assert result.drawdown_pct == 6
    broker.place_order.assert_not_called()
    broker.cancel_order.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("block", ["orders", "positions", "intents", "account"])
async def test_start_fails_without_changing_store(broker, monkeypatch, block):
    if block == "orders":
        broker.list_open_orders = AsyncMock(return_value=[object()])
    elif block == "positions":
        broker.list_positions = AsyncMock(
            return_value=[SimpleNamespace(qty=Decimal(1), avg_entry=Decimal(50))]
        )
    elif block == "intents":
        monkeypatch.setattr(
            "broker.switch_guard.broker_switch_blocked_reason", lambda: "unknown_intents:1"
        )
    with pytest.raises(HTTPException) as exc:
        await trading.start_risk_period(body("DUWRONG" if block == "account" else ACCOUNT))
    assert exc.value.status_code == 409
    with session_factory()() as session:
        assert list(session.scalars(select(RiskPeriodRow))) == []
    broker.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_suspend_keeps_exit_adapter_available(broker):
    await trading.start_risk_period(body())
    result = await trading.suspend_risk_period(
        trading.PaperRiskSuspendBody(
            account_id=ACCOUNT,
            confirmation="SUSPEND_PAPER_PERIOD",
        )
    )
    assert result.week_pnl is None
    assert result.drawdown_pct is None
    assert await broker.list_positions() == []
    assert await broker.list_open_orders() == []
    with pytest.raises(HTTPException):
        await trading.start_risk_period(body())
    broker.cancel_order.assert_not_called()


@pytest.mark.asyncio
async def test_risk_db_outage_does_not_reuse_previous_pass_or_break_portfolio(broker, monkeypatch):
    await trading.start_risk_period(body())

    def broken(*args):
        raise RuntimeError("simulated storage outage")

    monkeypatch.setattr("risk.paper_period.observe", broken)
    result = await broker.get_portfolio()
    assert result.equity == 100000
    assert result.week_pnl is None
    assert result.drawdown_pct is None
    assert result.risk_history_status == "unavailable"
    with pytest.raises(HTTPException):
        await trading.start_risk_period(body())
    broker.place_order.assert_not_called()


@pytest.mark.asyncio
async def test_alpaca_is_not_initialized_by_ibkr_endpoint(monkeypatch):
    from broker.paper.mock import MockPaperBroker

    monkeypatch.setattr(trading, "create_broker", lambda _: MockPaperBroker())
    with pytest.raises(HTTPException, match="IBKR_PAPER_REQUIRED"):
        await trading.start_risk_period(body())


@pytest.mark.asyncio
async def test_disconnected_broker_cannot_start_from_cached_summary(broker, monkeypatch):
    from core.enums import BrokerConnectionState

    monkeypatch.setattr(broker, "connection_state", lambda: BrokerConnectionState.DEGRADED)
    with pytest.raises(HTTPException, match="IBKR_NOT_READY"):
        await trading.start_risk_period(body())
    with session_factory()() as session:
        assert list(session.scalars(select(RiskPeriodRow))) == []


@pytest.mark.asyncio
async def test_corrupt_stored_period_does_not_reinitialize(broker):
    await trading.start_risk_period(body())
    with session_factory()() as session:
        row = session.scalar(select(RiskPeriodRow))
        payload = dict(row.payload)
        del payload["suspended"]
        row.payload = payload
        session.commit()
    snapshot = await broker.get_portfolio()
    assert snapshot.week_pnl is None
    assert snapshot.risk_history_status == "unavailable"
    with pytest.raises(HTTPException):
        await trading.start_risk_period(body())


def test_actual_observation_drives_weekly_risk_gate():
    from core.schemas import RiskLimits
    from risk.risk_engine import RiskEngine
    from tests.unit.test_capital_safety import _candidate, _portfolio

    now = datetime.now(UTC)
    start_period(ACCOUNT, "USD", Decimal(100000), now)
    period = observe(ACCOUNT, "USD", Decimal(95000), now + timedelta(seconds=1))
    portfolio = _portfolio(equity=Decimal(95000)).model_copy(update=period.metrics())
    result = RiskEngine(RiskLimits()).evaluate(_candidate(), portfolio)
    assert "MAX_WEEKLY_LOSS" in result.reasons


def test_initialized_period_passes_risk_when_other_facts_are_valid():
    from core.enums import EarningsCheck, NewsCheck, RiskVerdict, SectorCheck
    from core.schemas import RiskLimits
    from risk.risk_engine import RiskContext, RiskEngine
    from tests.unit.test_capital_safety import _candidate, _portfolio

    period = start_period(ACCOUNT, "USD", Decimal(100000), datetime.now(UTC))
    portfolio = _portfolio().model_copy(update=period.metrics())
    context = RiskContext(
        regime_tradable=True,
        earnings=EarningsCheck.CHECKED,
        news=NewsCheck.CHECKED,
        sector="technology",
        sector_check=SectorCheck.CHECKED,
    )
    decision = RiskEngine(RiskLimits()).evaluate(_candidate(), portfolio, context=context)
    assert decision.verdict is RiskVerdict.PASS
    assert decision.sized_qty > 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "accounts,configured", [(["DU1", "DU2"], None), (["DU1"], "DU2"), ([""], None)]
)
async def test_transport_refuses_ambiguous_account_summaries(monkeypatch, accounts, configured):
    from broker.ibkr.config import IBKRTransportConfig
    from broker.ibkr.live_transport import IBKRLiveTransport

    transport = IBKRLiveTransport(IBKRTransportConfig(account=configured))
    ib = SimpleNamespace(
        accountSummaryAsync=AsyncMock(
            return_value=[
                SimpleNamespace(account=a, tag="NetLiquidation", value="100000", currency="USD")
                for a in accounts
            ]
        )
    )
    monkeypatch.setattr(transport, "_ready", AsyncMock(return_value=ib))
    with pytest.raises(ValueError):
        await transport.account_summary()


@pytest.mark.asyncio
async def test_transport_default_account_filter_is_empty_not_literal_all(monkeypatch):
    from broker.ibkr.config import IBKRTransportConfig
    from broker.ibkr.live_transport import IBKRLiveTransport

    transport = IBKRLiveTransport(IBKRTransportConfig())
    ib = SimpleNamespace(
        accountSummaryAsync=AsyncMock(
            return_value=[
                SimpleNamespace(
                    account=ACCOUNT, tag="NetLiquidation", value="100000", currency="USD"
                ),
                SimpleNamespace(
                    account=ACCOUNT, tag="TotalCashValue", value="100000", currency="USD"
                ),
                SimpleNamespace(
                    account="All", tag="TotalCashValue", value="9999999", currency="USD"
                ),
            ]
        )
    )
    monkeypatch.setattr(transport, "_ready", AsyncMock(return_value=ib))
    summary = await transport.account_summary()
    ib.accountSummaryAsync.assert_awaited_once_with("")
    assert summary["Account"] == ACCOUNT
    assert summary["BaseCurrency"] == "USD"
    assert summary["TotalCashValue"] == "100000"


def test_migration_creates_and_removes_only_its_table(tmp_path):
    import importlib.util
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import create_engine, inspect

    path = Path(__file__).resolve().parents[2] / "alembic/versions/0016_risk_periods.py"
    spec = importlib.util.spec_from_file_location("risk_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()
        assert inspect(connection).get_pk_constraint("risk_periods")["constrained_columns"] == [
            "account_key"
        ]
        migration.downgrade()
        assert "risk_periods" not in inspect(connection).get_table_names()
