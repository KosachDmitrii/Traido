"""Whole-universe ORB selection, immutable persistence, and restart behavior."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from strategy.orb import PARAMETERS
from strategy.orb.runtime import discover
from strategy.orb.store import read_session, update_state
from tests.unit.test_orb_policy import NOW, evidence


class OpeningFeed:
    _feed = "sip"

    def __init__(self, symbols):
        self.symbols = symbols
        self.calls = []
        self.fail = False

    async def get_bars_batch(self, symbols, start, end, timeframe):
        self.calls.append((tuple(symbols), start, end, timeframe))
        if self.fail:
            raise RuntimeError("vendor unavailable")
        _, opening = evidence()
        result = {}
        for i, symbol in enumerate(self.symbols):
            bars = [b for b in opening if start <= b.ts <= end]
            result[symbol] = [
                b.model_copy(
                    update={
                        "symbol": symbol,
                        "volume": b.volume * (i + 1) if b.ts == opening[-1].ts else b.volume,
                    }
                )
                for b in bars
            ]
        return result


class Context:
    def __init__(self, symbols):
        self.symbols = symbols
        self.market_data = OpeningFeed(symbols)
        self.daily_calls = 0

    async def daily_bars(self, symbols, start, end):
        self.daily_calls += 1
        d, _ = evidence()
        return {s: [b.model_copy(update={"symbol": s}) for b in d] for s in symbols}


class Universe:
    def __init__(self, symbols):
        self.symbols = symbols
        self.calls = []

    async def get_scan_universe(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(symbols=self.symbols, total=len(self.symbols))


@pytest.mark.asyncio
async def test_all_eligible_names_are_read_and_top20_are_ranked_on_opening_volume():
    symbols = [f"S{i:03}" for i in range(75)]
    ctx, u = Context(symbols), Universe(symbols)
    result = await discover(ctx, u, now=NOW)
    assert u.calls[0]["max_size"] == 0
    assert ctx.daily_calls == 1
    assert len(ctx.market_data.calls) == 15
    assert all(end - start < timedelta(minutes=5) for _, start, end, _ in ctx.market_data.calls)
    assert set(result["plans"]) == set(symbols[-20:])
    assert result["counts"]["qualified"] == 75
    assert result["counts"]["selected"] == PARAMETERS["top_n"]
    assert all(s["state"] == "WAIT" for s in result["states"].values())
    # No broker, risk snapshot or account history was supplied. Observation is independent.


@pytest.mark.asyncio
async def test_restart_reuses_saved_selection_and_never_moves_the_range():
    symbols = ["AAPL", "MSFT"]
    first = await discover(Context(symbols), Universe(symbols), now=NOW)
    restarted = Context(["DIFFERENT"])
    after = await discover(restarted, Universe(["DIFFERENT"]), now=NOW + timedelta(hours=1))
    assert after == first
    assert restarted.daily_calls == 0 and restarted.market_data.calls == []


@pytest.mark.asyncio
async def test_vendor_failure_does_not_freeze_an_empty_selection():
    ctx = Context(["AAPL"])
    ctx.market_data.fail = True
    with pytest.raises(RuntimeError):
        await discover(ctx, Universe(["AAPL"]), now=NOW)
    assert read_session("2026-09-09") is None
    from strategy.orb.runtime import STATUS

    assert STATUS["status"] == "data_blocked"
    assert STATUS["reason"] == "ORB_SERVICE_UNAVAILABLE"
    ctx.market_data.fail = False
    assert (await discover(ctx, Universe(["AAPL"]), now=NOW))["plans"]


@pytest.mark.asyncio
async def test_observation_update_cannot_erase_a_consumed_plan():
    await discover(Context(["AAPL"]), Universe(["AAPL"]), now=NOW)
    update_state("2026-09-09", "AAPL", {"state": "BUY_ALLOWED", "opportunity_id": "one-attempt"})
    update_state("2026-09-09", "AAPL", {"state": "WAIT"})
    assert read_session("2026-09-09")["states"]["AAPL"]["opportunity_id"] == "one-attempt"


@pytest.mark.asyncio
async def test_iex_is_visible_data_block_and_never_a_fake_no_setup():
    ctx = Context(["AAPL"])
    ctx.market_data._feed = "iex"
    result = await discover(ctx, Universe(["AAPL"]), now=NOW)
    assert result["status"] == "data_blocked"
    assert result["reason"] == "ORB_SIP_REQUIRED"
    assert read_session("2026-09-09") is None
    assert ctx.daily_calls == 0
