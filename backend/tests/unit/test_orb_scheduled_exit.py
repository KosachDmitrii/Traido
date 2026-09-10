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
