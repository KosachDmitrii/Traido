"""Rolling ranges add late candidates without replacing claims or skipping admission."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D

import pytest

from strategy.orb import INTRADAY_VERSION, form_plan
from strategy.orb.intraday import refresh
from strategy.orb.retest import rebuild
from strategy.orb.store import merge_intraday_discovery, read_session, update_state
from tests.unit.test_orb_policy import NOW, evidence, quote
from tests.unit.test_orb_retest import scenario
from tests.unit.test_orb_session import Context, Universe


def window_evidence():
    daily, opening = evidence()
    return daily, [b.model_copy(update={"ts": b.ts + timedelta(minutes=25)}) for b in opening]


def test_same_clock_volume_and_immutable_range_replay():
    daily, windows = window_evidence()
    plan = form_plan("AAPL", daily, windows, now=NOW, feed="sip", version=INTRADAY_VERSION).plan
    assert plan.relative_volume == 2
    assert plan.range_start == windows[-1].ts
    _, rows, _ = scenario()
    rows = [b.model_copy(update={"ts": b.ts + timedelta(minutes=25)}) for b in rows]
    now = rows[-1].ts + timedelta(minutes=5)
    ready = rebuild(plan, rows, now=now).plan
    assert ready.evidence["retest"]
    assert ready.range_start == plan.range_start
    assert rebuild(ready, rows, now=now).plan == ready
    from strategy.orb import evaluate_trigger

    assert evaluate_trigger(ready, quote("101.10", "101.12", now), now=now).state == "BUY_ALLOWED"
    assert (
        evaluate_trigger(
            ready, quote("101.10", "101.12", now - timedelta(seconds=6)), now=now
        ).state
        == "DATA_BLOCKED"
    )


@pytest.mark.parametrize(
    "mode,reason",
    [
        ("missing", "ORB_HISTORY_INCOMPLETE"),
        ("wrong_clock", "ORB_HISTORY_INCOMPLETE"),
        ("future", "ORB_OPENING_RANGE_FORMING"),
        ("low_volume", "ORB_RELATIVE_VOLUME_LOW"),
        ("red", "ORB_OPENING_NOT_BULLISH"),
    ],
)
def test_intraday_does_not_relax_selection_or_use_future_bars(mode, reason):
    daily, windows = window_evidence()
    now = NOW
    if mode == "missing":
        windows = windows[1:]
    elif mode == "wrong_clock":
        windows[0] = windows[0].model_copy(update={"ts": windows[0].ts - timedelta(minutes=5)})
    elif mode == "future":
        now = windows[-1].ts + timedelta(minutes=4)
    elif mode == "low_volume":
        windows[-1] = windows[-1].model_copy(update={"volume": D(99999)})
    else:
        windows[-1] = windows[-1].model_copy(update={"close": D(99)})
    decision = form_plan("AAPL", daily, windows, now=now, feed="sip", version=INTRADAY_VERSION)
    assert decision.plan is None
    assert reason in decision.reasons


@pytest.mark.asyncio
async def test_late_symbol_added_once_and_existing_claim_preserved():
    from strategy.orb.runtime import discover

    ctx = Context(["AAPL"])
    original = await discover(ctx, Universe(["AAPL"]), now=NOW)
    update_state(original["session"], "AAPL", {"state": "EXECUTED", "opportunity_id": "claimed"})
    daily, windows = window_evidence()
    daily = [b.model_copy(update={"symbol": "GOOGL"}) for b in daily]
    windows = [b.model_copy(update={"symbol": "GOOGL"}) for b in windows]
    pool = deepcopy(original["discovery_pool"])
    pool["GOOGL"] = {
        "daily": [b.model_dump(mode="json") for b in daily],
        "instrument": {"asset_class": "stock", "provider": "alpaca"},
    }
    # Seed a pre-existing session without a pool to exercise durable initialization.
    from database.models.orb import OrbSessionRow
    from database.session import session_factory

    with session_factory()() as db:
        row = db.get(OrbSessionRow, original["session"])
        payload = deepcopy(row.payload)
        payload.pop("discovery_pool")
        row.payload = payload
        db.commit()
    merge_intraday_discovery(original["session"], pool=pool)

    class Feed:
        _feed = "sip"
        calls = 0

        async def get_bars_batch(self, symbols, start, end, timeframe):
            self.calls += 1
            return {
                s: [b for b in windows if start <= b.ts <= end and b.symbol == s] for s in symbols
            }

    ctx.market_data = Feed()
    result = await refresh(ctx, Universe([]), now=NOW)
    assert result["intraday_discovery"]["added"] == ["GOOGL"]
    assert result["plans"]["AAPL"] == original["plans"]["AAPL"]
    assert result["states"]["AAPL"]["opportunity_id"] == "claimed"
    assert result["states"]["GOOGL"]["state"] == "WAIT"
    assert ctx.market_data.calls == 15
    await refresh(ctx, Universe([]), now=NOW + timedelta(seconds=30))
    assert ctx.market_data.calls == 15
    assert read_session(original["session"])["plans"]["GOOGL"]["version"] == INTRADAY_VERSION


@pytest.mark.asyncio
async def test_discovery_failure_preserves_existing_positions_and_retries():
    from strategy.orb.runtime import discover

    ctx = Context(["AAPL"])
    original = await discover(ctx, Universe(["AAPL"]), now=NOW)
    # Make an additional candidate with real captured test history.
    from database.models.orb import OrbSessionRow
    from database.session import session_factory

    with session_factory()() as db:
        row = db.get(OrbSessionRow, original["session"])
        payload = deepcopy(row.payload)
        payload["discovery_pool"]["GOOGL"] = payload["discovery_pool"]["AAPL"]
        row.payload = payload
        db.commit()
    ctx.market_data.fail = True
    result = await refresh(ctx, Universe([]), now=NOW)
    assert result["intraday_discovery"]["status"] == "data_blocked"
    assert result["plans"] == original["plans"]
    calls = len(ctx.market_data.calls)
    await refresh(ctx, Universe([]), now=NOW + timedelta(seconds=10))
    assert len(ctx.market_data.calls) == calls
