from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from database.models.risk_period import RiskPeriodRow
from database.session import get_sync_engine, session_factory
from risk.paper_period import RiskPeriodError, observe, start_period, suspend_period

NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)
ACCOUNT = "alpaca-account-123456"


def start():
    return start_period(ACCOUNT, "USD", Decimal(100000), NOW)


def test_reads_do_not_initialize():
    assert observe(ACCOUNT, "USD", Decimal(100000), NOW) is None


def test_start_has_truthful_partial_period_and_zero_initial_change():
    period = start()
    assert period.started_at == NOW
    assert period.metrics()["week_pnl"] == 0
    assert period.metrics()["drawdown_pct"] == 0
    assert period.metrics()["risk_history_status"] == "observed_period"


def test_restart_and_duplicate_start_preserve_loss_and_high_water():
    original = start()
    observe(ACCOUNT, "USD", Decimal(110000), NOW + timedelta(minutes=1))
    get_sync_engine.cache_clear()
    period = observe(ACCOUNT, "USD", Decimal(99000), NOW + timedelta(minutes=2))
    assert period.id == original.id
    assert period.metrics()["week_pnl"] == -1000
    assert period.metrics()["drawdown_pct"] == 10
    retry = start_period(ACCOUNT, "USD", Decimal(99000), NOW + timedelta(minutes=3))
    assert retry.high_water == 110000
    assert retry.initial_equity == 100000


def test_account_isolation():
    start()
    assert observe("DUOTHER", "USD", Decimal(50000), NOW) is None


@pytest.mark.parametrize("account,currency", [("", "USD"), (ACCOUNT, "EUR")])
def test_invalid_account_or_currency_rejected(account, currency):
    with pytest.raises(RiskPeriodError):
        start_period(account, currency, Decimal(100000), NOW)


@pytest.mark.parametrize("equity", ["NaN", "Infinity", "0", "-1"])
def test_invalid_equity_cannot_start_or_replace_period(equity):
    start()
    with pytest.raises(RiskPeriodError):
        observe(ACCOUNT, "USD", Decimal(equity), NOW)


def test_backwards_clock_cannot_overwrite_newer_observation():
    start()
    with pytest.raises(RiskPeriodError):
        observe(ACCOUNT, "USD", Decimal(200000), NOW - timedelta(seconds=1))


def test_week_roll_keeps_gap_loss_and_lifetime_high_water():
    start()
    friday = datetime(2026, 9, 11, 20, 0, tzinfo=UTC)
    observe(ACCOUNT, "USD", Decimal(105000), friday)
    monday = datetime(2026, 9, 14, 14, 0, tzinfo=UTC)
    period = observe(ACCOUNT, "USD", Decimal(100000), monday)
    assert period.week_baseline_at == friday
    assert period.metrics()["week_pnl"] == -5000
    assert period.high_water == 105000


def test_exchange_week_does_not_roll_at_utc_midnight():
    start()
    sunday_evening_et = datetime(2026, 9, 14, 1, 0, tzinfo=UTC)
    period = observe(ACCOUNT, "USD", Decimal(98000), sunday_evening_et)
    assert period.week_baseline_at == NOW
    assert period.metrics()["week_pnl"] == -2000


def test_next_day_preserves_weekly_loss_but_starts_daily_change_at_last_observation():
    start()
    observe(ACCOUNT, "USD", Decimal(98000), NOW + timedelta(minutes=1))
    next_day = observe(ACCOUNT, "USD", Decimal(97000), NOW + timedelta(days=1))
    assert next_day.metrics()["week_pnl"] == -3000
    assert next_day.metrics()["day_pnl"] == -1000
    assert next_day.metrics()["drawdown_pct"] == 3


def test_suspend_blocks_metrics_and_cannot_be_restarted():
    start()
    suspend_period(ACCOUNT, "USD")
    period = observe(ACCOUNT, "USD", Decimal(1000000), NOW + timedelta(minutes=1))
    assert period.metrics()["week_pnl"] is None
    assert period.metrics()["drawdown_pct"] is None
    with pytest.raises(RiskPeriodError, match="SUSPENDED"):
        start_period(ACCOUNT, "USD", Decimal(1000000), NOW)


def test_concurrent_start_is_one_baseline():
    with ThreadPoolExecutor(max_workers=4) as pool:
        periods = list(pool.map(lambda _: start(), range(8)))
    assert len({p.id for p in periods}) == 1
    with session_factory()() as session:
        assert len(list(session.scalars(select(RiskPeriodRow)))) == 1


@pytest.mark.asyncio
async def test_adapter_observes_started_period_and_isolates_unknown_account():
    from broker.interface import BrokerUnreachable
    from tests.alpaca_account import account_broker

    # Adapter uses real UTC; start one minute earlier for monotonic ordering.
    start_period(ACCOUNT, "USD", Decimal(100000), datetime.now(UTC) - timedelta(minutes=1))
    summary = {
        "id": ACCOUNT,
        "currency": "USD",
        "equity": "99000",
        "cash": "99000",
    }
    broker = account_broker(summary)
    snapshot = await broker.get_portfolio()
    assert snapshot.week_pnl == -1000
    assert snapshot.drawdown_pct == 1
    assert snapshot.risk_account_id == ACCOUNT
    summary["id"] = "DUOTHER"
    with pytest.raises(BrokerUnreachable, match="ACCOUNT_CHANGED"):
        await broker.get_portfolio(fresh=True)
