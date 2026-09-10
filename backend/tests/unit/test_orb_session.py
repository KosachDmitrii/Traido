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
async def test_unknown_feed_is_visible_data_block_and_never_a_fake_no_setup():
    ctx = Context(["AAPL"])
    ctx.market_data._feed = "otc"
    result = await discover(ctx, Universe(["AAPL"]), now=NOW)
    assert result["status"] == "data_blocked"
    assert result["reason"] == "ORB_UNSUPPORTED_FEED"
    assert read_session("2026-09-09") is None
    assert ctx.daily_calls == 0


@pytest.mark.asyncio
async def test_paper_rollout_changes_only_unpublished_limits_and_records_old_plan():
    from copy import deepcopy
    from decimal import ROUND_FLOOR, Decimal

    from database.models.orb import OrbSessionRow
    from database.session import session_factory
    from strategy.orb.store import upgrade_unpublished_entry_limits

    original = await discover(Context(["AAPL", "MSFT"]), Universe(["AAPL", "MSFT"]), now=NOW)
    legacy = deepcopy(original)
    legacy.pop("entry_policy_rollout")
    legacy.pop("entry_policy_changes")
    for plan in legacy["plans"].values():
        trigger, stop = Decimal(plan["trigger"]), Decimal(plan["stop"])
        plan["version"] = "orb@1.1.0"
        plan["max_entry"] = str(
            (trigger + (trigger - stop) * Decimal("0.25")).quantize(
                Decimal("0.01"), rounding=ROUND_FLOOR
            )
        )
        plan["evidence"]["parameters"].pop("entry_policy_revision", None)
        plan["evidence"]["parameters"]["max_entry_drift_r"] = "0.25"
    legacy["states"]["MSFT"]["opportunity_id"] = "already-published"
    with session_factory()() as db:
        row = db.get(OrbSessionRow, "2026-09-09")
        row.payload = legacy
        db.commit()
    upgraded = upgrade_unpublished_entry_limits("2026-09-09", now=NOW)
    assert upgraded["plans"]["MSFT"] == legacy["plans"]["MSFT"]
    before, after = legacy["plans"]["AAPL"], upgraded["plans"]["AAPL"]
    assert Decimal(after["max_entry"]) > Decimal(before["max_entry"])
    assert after["stop"] == before["stop"]
    assert after["trigger"] == before["trigger"]
    assert after["evidence"]["entry_policy_change"]["old_max_entry"] == before["max_entry"]
    assert upgrade_unpublished_entry_limits("2026-09-09", now=NOW) == upgraded
