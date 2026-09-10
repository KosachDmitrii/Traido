"""Time exits use the existing execution owner and never adopt old positions."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from strategy.orb.loop import exit_due_positions
from tests.unit.test_orb_policy import plan


@pytest.mark.asyncio
async def test_time_exit_only_runs_when_due_and_preserves_manual_positions(monkeypatch):
    from api import deps
    from trading.ledger import LEDGER

    p = plan()
    rows = [
        SimpleNamespace(
            symbol="AAPL",
            payload={"exit_policy": "session_close", "exit_at": p.exit_at.isoformat()},
        ),
        SimpleNamespace(symbol="BAC", payload={}),
    ]
    monkeypatch.setattr(LEDGER, "get_open", lambda: rows)
    calls = []

    class Execution:
        async def close_position(self, symbol, *, reason):
            calls.append((symbol, reason))

    monkeypatch.setattr(deps, "build_execution_service", lambda: Execution())
    assert await exit_due_positions(now=p.exit_at - timedelta(seconds=1)) == 0
    assert calls == []
    assert await exit_due_positions(now=p.exit_at) == 1
    assert calls == [("AAPL", "ORB_SESSION_END")]


@pytest.mark.asyncio
async def test_an_overdue_exit_is_retried_after_broker_failure(monkeypatch):
    from api import deps
    from trading.ledger import LEDGER

    p = plan()
    monkeypatch.setattr(
        LEDGER,
        "get_open",
        lambda: [
            SimpleNamespace(
                symbol="AAPL",
                payload={"exit_policy": "session_close", "exit_at": p.exit_at.isoformat()},
            )
        ],
    )
    calls = []

    class Execution:
        async def close_position(self, symbol, *, reason):
            calls.append(symbol)
            if len(calls) == 1:
                raise RuntimeError("broker unavailable")

    monkeypatch.setattr(deps, "build_execution_service", lambda: Execution())
    assert await exit_due_positions(now=p.exit_at + timedelta(hours=1)) == 0
    assert await exit_due_positions(now=p.exit_at + timedelta(hours=1, seconds=5)) == 1
    assert calls == ["AAPL", "AAPL"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "minutes,bid,stale,expected",
    [
        (1, "102.50", False, "ORB_TARGET_REACHED"),
        (29, "101.00", False, None),
        (30, "101.00", False, "ORB_TIME_NO_PROGRESS"),
        (30, "101.12", False, "ORB_TIME_NO_PROGRESS"),
        (30, "101.13", False, None),
        (30, "102.50", True, None),
    ],
)
async def test_retest_exit_target_time_and_quote_freshness(
    monkeypatch, minutes, bid, stale, expected
):
    from decimal import Decimal as D

    from api import deps
    from core.audit import InMemoryAudit
    from tests.unit.test_orb_policy import quote
    from tests.unit.test_orb_retest import ready_plan
    from trading.ledger import LEDGER

    p, _, opened = ready_plan()
    now = opened + timedelta(minutes=minutes)
    row = SimpleNamespace(
        symbol="AAPL",
        qty=D(5),
        avg_entry=D("101.12"),
        opened_at=opened,
        payload={
            "exit_policy": "session_close",
            "exit_at": p.exit_at.isoformat(),
            "orb_plan": p.model_dump(mode="json"),
        },
    )
    monkeypatch.setattr(LEDGER, "get_open", lambda: [row])
    calls = []

    class Execution:
        quotes = None
        audit = InMemoryAudit()

        async def get_quote(self, symbol):
            return quote(bid, str(D(bid) + D("0.02")), now - timedelta(seconds=6 if stale else 0))

        async def close_position(self, symbol, *, reason):
            calls.append((symbol, reason))

    execution = Execution()
    execution.quotes = execution
    monkeypatch.setattr(deps, "build_execution_service", lambda: execution)
    assert await exit_due_positions(now=now) == int(expected is not None)
    assert calls == ([("AAPL", expected)] if expected else [])
