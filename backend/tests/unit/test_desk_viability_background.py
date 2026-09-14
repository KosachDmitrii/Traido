"""The light desk never waits on Alpaca quote enrichment."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

import trading.desk_viability as module


class _Opportunity:
    def __init__(self, symbol: str) -> None:
        self.id = uuid4()
        self.candidate = SimpleNamespace(symbol=symbol)

    def model_dump(self, *, mode: str) -> dict:
        assert mode == "json"
        return {"id": str(self.id), "candidate": {"symbol": self.candidate.symbol}}


@pytest.mark.asyncio
async def test_uncached_viability_returns_unverified_while_refresh_runs(monkeypatch) -> None:
    module.clear_viability_cache()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_refresh(opportunities):
        assert len(opportunities) == 2
        started.set()
        await release.wait()
        return []

    monkeypatch.setattr(module, "attach_buy_viability", slow_refresh)
    rows = module.attach_cached_buy_viability([_Opportunity("AAPL"), _Opportunity("MSFT")])

    assert [row["viability"]["buyable"] for row in rows] == [False, False]
    assert all(row["viability"]["reasons"] == ["LIVE_QUOTE_REFRESHING"] for row in rows)
    await asyncio.wait_for(started.wait(), timeout=0.1)

    task = module._refresh_task
    assert task is not None and not task.done()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    module.clear_viability_cache()
