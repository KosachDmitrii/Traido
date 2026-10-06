"""Observer isolation, honest evidence and independent forward cohorts."""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select

from core.clock import ET
from database.base import Base
from database.models.desk import AdmissionRecordRow, DecisionOutcomeRow, OrderIntentRow
from database.models.journal import TradeJournalRow
from database.models.monitoring import MonitoringSampleRow
from database.session import init_db, session_factory
from monitoring import service
from monitoring.service import (
    build_report,
    check_entry,
    completed_sessions,
    record_sample,
    refresh_report,
    saved_report,
)
from strategy.orb import INTRADAY_VERSION, VERSION

NOW = datetime(2026, 10, 6, 17, tzinfo=ET)


@pytest.fixture
def engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'monitor.db'}")
    init_db(engine)
    yield engine
    engine.dispose()


def payload(qty="1", price="20", equity="500", cash="100"):
    return {
        "requested_qty": qty,
        "limit_price": price,
        "risk_snapshot": {
            "sized_qty": "1.25",
            "limits_applied": {"max_position_pct": "5"},
            "portfolio": {"equity": equity, "cash": cash},
        },
    }


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"qty": "1.1"}, "WHOLE_SHARE_QUANTITY_VIOLATION"),
        ({"cash": "19"}, "ENTRY_EXCEEDS_SAVED_CASH"),
        ({"price": "25.01"}, "ENTRY_EXCEEDS_SAVED_POSITION_CAP"),
        ({"qty": "2"}, "ENTRY_EXCEEDS_SAVED_RISK_SIZE"),
    ],
)
def test_saved_entry_violations(changes, reason):
    check = check_entry(payload(**changes))
    assert check["status"] == "failed"
    assert reason in check["reasons"]


def test_budget_boundary_missing_and_small_accounts():
    assert check_entry(payload(price="25"))["status"] == "passed"
    assert check_entry(payload(equity="5000", cash="1000"))["status"] == "passed"
    assert check_entry(payload(price="251", equity="5000", cash="1000"))["status"] == "failed"
    assert check_entry({})["status"] == "insufficient_data"
    assert check_entry(payload(price="NaN"))["status"] == "insufficient_data"


def operational_facts(engine):
    with engine.connect() as conn:
        return {
            table.name: [tuple(row) for row in conn.execute(select(table))]
            for table in Base.metadata.sorted_tables
            if not table.name.startswith("monitoring_")
        }


def test_observer_never_changes_trading_or_calls_broker(engine, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("observer reached a broker factory")

    monkeypatch.setattr("broker.factory.create_broker", forbidden)
    aid = uuid4()
    with session_factory(engine)() as db:
        db.add(
            AdmissionRecordRow(
                id=aid, symbol="TEST", recorded_at=NOW, decision="BUY_ALLOWED", payload={}
            )
        )
        db.add(
            OrderIntentRow(
                idempotency_key="entry:monitor",
                purpose="entry",
                broker="ALPACA",
                broker_account_id="test",
                broker_environment="paper",
                symbol="TEST",
                status="created",
                created_at=NOW,
                approval_admission_record_id=aid,
                geometry_hash="a" * 64,
                request_id="a" * 32,
                request_fingerprint="b" * 64,
                payload=payload(),
            )
        )
        db.add(
            DecisionOutcomeRow(
                symbol="TEST",
                stage="approval",
                outcome="WAIT",
                primary_reason="ORB_WAITING_PULLBACK",
                recorded_at=NOW,
                payload={},
            )
        )
        db.commit()
    before = operational_facts(engine)
    assert record_sample(engine=engine, now=NOW)
    assert not record_sample(engine=engine, now=NOW + timedelta(seconds=1))
    refresh_report(engine=engine, now=NOW)
    report = saved_report(engine=engine, now=NOW)
    assert report["available"] and not report["stale"]
    assert report["budget_checks"] == {"passed": 1}
    assert report["budget_status"] == "insufficient_data"
    assert report["funnel"][0]["count"] == 1
    assert operational_facts(engine) == before
    assert saved_report(engine=engine, now=NOW + timedelta(minutes=11))["stale"]


def test_empty_report_never_claims_success(engine):
    report = build_report(engine=engine, now=NOW)
    assert (
        report["technical_status"]
        == report["budget_status"]
        == report["strategy_status"]
        == "insufficient_data"
    )
    assert report["complete_sessions"] == 0
    assert len(report["daily"]) == 30
    assert report["account"] is None
    assert not report["live_ready"]


def test_calendar_uses_completed_exchange_sessions_and_early_close():
    days = completed_sessions(datetime(2026, 10, 6, 12, tzinfo=ET))
    assert days[0] == date(2026, 8, 24) and days[-1] == date(2026, 10, 5)
    assert date(2026, 9, 7) not in days
    assert completed_sessions(datetime(2026, 11, 27, 13, tzinfo=ET))[-1] == date(2026, 11, 27)
    assert completed_sessions(datetime(2026, 10, 7, 1, tzinfo=UTC))[-1] == date(2026, 10, 6)


def test_policy_change_and_versions_do_not_merge_evidence(engine, monkeypatch):
    h, policy = service.policy_identity()
    start = datetime(2026, 10, 5, 9, 30, tzinfo=ET)
    with session_factory(engine)() as db:
        for moment, key in [
            (start - timedelta(days=3), "old"),
            (start, h),
            (start + timedelta(minutes=1), h),
        ]:
            db.add(
                MonitoringSampleRow(
                    minute=moment,
                    session=str(moment.date()),
                    policy_hash=key,
                    payload={"problems": [], "account": None},
                )
            )
        for version, backtest, pnl, opened in [
            (VERSION, None, 10, start),
            (INTRADAY_VERSION, None, -4, start),
            ("old", None, 999, start),
            (VERSION, uuid4(), 999, start),
            (VERSION, None, None, start),
            (VERSION, None, 999, start - timedelta(days=1)),
        ]:
            db.add(
                TradeJournalRow(
                    symbol="TEST",
                    entry=20,
                    exit=21,
                    qty=1,
                    pnl=pnl,
                    strategy_version=version,
                    backtest_run_id=backtest,
                    opened_at=opened,
                    closed_at=start + timedelta(hours=1),
                )
            )
        db.commit()
    report = build_report(engine=engine, now=NOW)
    assert [s["gross_closed_pnl"] for s in report["strategies"]] == ["10.0000", "-4.0000"]
    assert report["daily"][-2]["unverified_trades"] == 1
    assert all(s["net_pnl"] is None for s in report["strategies"])
    monkeypatch.setattr(service, "policy_identity", lambda: ("new", policy))
    changed = build_report(engine=engine, now=NOW)
    assert changed["cohort_started_at"] is None
    assert all(s["closed_trades"] == 0 for s in changed["strategies"])


def test_coverage_requires_open_close_and_no_gaps(engine):
    h, _ = service.policy_identity()
    day = date(2026, 10, 5)
    start = datetime(2026, 10, 5, 9, 30, tzinfo=ET)
    samples = [
        MonitoringSampleRow(
            minute=start + timedelta(minutes=m), session=str(day), policy_hash=h, payload={}
        )
        for m in range(391)
    ]
    assert service._coverage(samples, day)["complete"]
    assert not service._coverage(samples[10:], day)["complete"]
    assert not service._coverage(samples[:100] + samples[105:], day)["complete"]


async def test_observer_survives_failure_and_stops(monkeypatch):
    import asyncio

    from monitoring import loop

    done = asyncio.Event()
    calls = 0

    def sample():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("secret should never appear in status")

    def report():
        done_loop.call_soon_threadsafe(done.set)

    done_loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "record_sample", sample)
    monkeypatch.setattr(loop, "refresh_report", report)
    task = asyncio.create_task(loop._run(0.01))
    await asyncio.wait_for(done.wait(), 1)

    async def recovered():
        while loop.status()["last_error"] is not None:
            await asyncio.sleep(0.001)

    await asyncio.wait_for(recovered(), 1)
    assert calls >= 2 and loop.status()["last_error"] is None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_monitoring_migration_round_trip():
    import runpy

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    engine = create_engine("sqlite://")
    migration = runpy.run_path("alembic/versions/0022_monitoring_samples.py")
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        migration["upgrade"]()
        assert set(inspect(conn).get_table_names()) == {"monitoring_samples", "monitoring_reports"}
        migration["downgrade"]()
        assert inspect(conn).get_table_names() == []


async def test_shutdown_drains_inflight_tick(monkeypatch):
    import asyncio
    import threading

    from monitoring import loop

    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow():
        entered.set()
        release.wait(1)
        finished.set()

    task = asyncio.create_task(loop._in_thread(slow))
    assert await asyncio.to_thread(entered.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


def test_daily_history_keeps_legacy_losses_and_unknown_days(engine):
    start = datetime(2026, 10, 5, 9, 30, tzinfo=ET)
    h, _ = service.policy_identity()
    with session_factory(engine)() as db:
        db.add(
            MonitoringSampleRow(
                minute=start,
                session="2026-10-05",
                policy_hash=h,
                payload={"problems": [], "account": None},
            )
        )
        for version, pnl in [("legacy@1", -20), (VERSION, 3)]:
            db.add(
                TradeJournalRow(
                    symbol="TEST",
                    entry=20,
                    exit=21,
                    qty=1,
                    pnl=pnl,
                    strategy_version=version,
                    opened_at=start,
                    closed_at=start + timedelta(hours=1),
                )
            )
        db.commit()
    report = build_report(engine=engine, now=NOW)
    day = report["daily"][-2]
    assert day["closed_trades"] == 2 and day["gross_closed_pnl"] == "-17.0000"
    assert report["strategies"][0]["gross_closed_pnl"] == "3.0000"
    assert report["daily"][-1]["gross_closed_pnl"] is None
