"""Completed-bar evidence, immutable geometry, expiry and no chasing for v2."""

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal as D

import pytest

from core.enums import Timeframe
from core.schemas import Bar
from strategy.orb import OrbPlan, evaluate_trigger, form_plan
from strategy.orb.retest import rebuild
from tests.unit.test_orb_policy import NOW, evidence, quote


def scenario(now=NOW):
    daily, opening = evidence(now)
    base = form_plan("AAPL", daily, opening, now=now, feed="sip").plan
    assert base is not None
    start = base.range_end

    def bar(i, o, h, l, c):
        return Bar(
            symbol="AAPL",
            timeframe=Timeframe.M5,
            ts=start + timedelta(minutes=i * 5),
            open=D(o),
            high=D(h),
            low=D(l),
            close=D(c),
            volume=D(100000),
            source="alpaca",
        )

    rows = [
        bar(0, "101.05", "102.5", "101.02", "101.5"),
        bar(1, "101.5", "101.6", "100.95", "101.02"),
        bar(2, "101.02", "101.18", "100.98", "101.12"),
    ]
    return base, rows, start + timedelta(minutes=15)


def ready_plan():
    base, rows, now = scenario()
    result = rebuild(base, rows, now=now)
    assert result.reasons == ["ORB_RETEST_CONFIRMED"]
    return result.plan, rows, now


def test_requires_distinct_completed_breakout_retest_and_confirmation():
    base, rows, now = scenario()
    for count, reason in [
        (0, "ORB_RETEST_WAIT_BREAKOUT"),
        (1, "ORB_RETEST_WAIT_RETURN"),
        (2, "ORB_RETEST_WAIT_CONFIRMATION"),
    ]:
        result = rebuild(base, rows, now=base.range_end + timedelta(minutes=count * 5))
        assert result.reasons == [reason]
        assert (
            evaluate_trigger(
                result.plan,
                quote(ts=base.range_end + timedelta(minutes=count * 5)),
                now=base.range_end + timedelta(minutes=count * 5),
            ).state
            != "BUY_ALLOWED"
        )
    result = rebuild(base, rows, now=now)
    p = result.plan
    assert p.trigger == D("101.01") and p.max_entry == D("101.12")
    assert p.stop == D("100.87")  # observed retest low 100.95 minus 0.02 * daily ATR 4
    assert D(p.evidence["retest"]["target"]) == D("102.50")  # observed BEFORE retest
    assert evaluate_trigger(p, quote("101.10", "101.12", now), now=now).state == "BUY_ALLOWED"
    restored = OrbPlan.model_validate(p.model_dump(mode="json"))
    assert rebuild(restored, rows, now=now).plan == p


@pytest.mark.parametrize(
    "bid,ask,reason",
    [
        ("101.11", "101.13", "ORB_WAITING_PULLBACK"),
        ("101.00", "101.02", "ORB_RETEST_WAIT_RECOVERY"),
        ("100.87", "100.89", "ORB_STOP_BREACHED"),
    ],
)
def test_no_chasing_or_buying_a_broken_setup(bid, ask, reason):
    p, _, now = ready_plan()
    assert evaluate_trigger(p, quote(bid, ask, now), now=now).reasons == [reason]
    assert (
        evaluate_trigger(p, quote("101.10", "101.12", now), now=now, limit_price=D("101.13")).state
        != "BUY_ALLOWED"
    )


def test_signal_expires_even_if_price_is_still_in_zone():
    p, rows, now = ready_plan()
    expiry = now + timedelta(minutes=10)
    assert evaluate_trigger(p, quote("101.1", "101.12", expiry), now=expiry).reasons == [
        "ORB_RETEST_EXPIRED"
    ]
    for i in (0, 1):
        rows.append(
            rows[-1].model_copy(
                update={
                    "ts": now + timedelta(minutes=i * 5),
                    "low": D("101.02"),
                    "open": D("101.03"),
                }
            )
        )
    result = rebuild(p, rows, now=expiry)
    assert "retest" not in result.plan.evidence


@pytest.mark.parametrize(
    "kind", ["gap", "duplicate", "stale", "naive", "wrong_symbol", "wrong_source"]
)
def test_missing_or_unreliable_history_never_authorizes_entry(kind):
    p, rows, now = ready_plan()
    if kind == "gap":
        rows.pop(1)
    elif kind == "duplicate":
        rows.append(rows[0])
    elif kind == "stale":
        rows = []
    elif kind == "naive":
        rows[0] = rows[0].model_copy(update={"ts": rows[0].ts.replace(tzinfo=None)})
    elif kind == "wrong_symbol":
        rows[0] = rows[0].model_copy(update={"symbol": "MSFT"})
    else:
        rows[0] = rows[0].model_copy(update={"source": "synthetic"})
    result = rebuild(p, rows, now=now)
    assert result.state == "DATA_BLOCKED"
    assert not result.plan.evidence.get("retest")


@pytest.mark.parametrize("low,high", [("100.80", "101.18"), ("101.0", "102.51")])
def test_stop_or_target_touched_after_confirmation_invalidates_old_signal(low, high):
    p, rows, now = ready_plan()
    rows.append(rows[-1].model_copy(update={"ts": now, "low": D(low), "high": D(high)}))
    result = rebuild(p, rows, now=now + timedelta(minutes=5))
    assert "retest" not in result.plan.evidence


def test_no_manufactured_target_to_rescue_poor_reward():
    base, rows, now = scenario()
    rows[0] = rows[0].model_copy(update={"high": D("101.6")})
    result = rebuild(base, rows, now=now)
    assert result.reasons == ["ORB_RETEST_REWARD_INSUFFICIENT"]
    assert "retest" not in result.plan.evidence


def test_skip_barrier_cannot_reuse_an_old_confirmation():
    p, rows, now = ready_plan()
    result = rebuild(p, rows, now=now, after=now + timedelta(seconds=60))
    assert "retest" not in result.plan.evidence


@pytest.mark.parametrize("value", ["NaN", "Infinity", "100", None])
def test_malformed_target_blocks_before_execution(value):
    p, _, now = ready_plan()
    raw = deepcopy(p.evidence)
    raw["retest"]["target"] = value
    altered = p.model_copy(update={"evidence": raw})
    assert evaluate_trigger(altered, quote("101.1", "101.12", now), now=now).state == "DATA_BLOCKED"
