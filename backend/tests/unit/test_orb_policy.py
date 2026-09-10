"""Market evidence and capital-facing ORB invariants, not score thresholds."""

from datetime import UTC, datetime, time, timedelta
from decimal import Decimal as D

import pytest
from pydantic import ValidationError

from core.clock import ET
from core.enums import Timeframe
from core.schemas import Bar, Quote
from strategy.orb import OrbPlan, _previous_sessions, evaluate_trigger, form_plan

NOW = datetime(2026, 9, 9, 14, 0, tzinfo=UTC)


def evidence(now=NOW):
    daily, opening = [], []
    for day in _previous_sessions(now, 15):
        daily.append(
            Bar(
                symbol="AAPL",
                timeframe=Timeframe.D1,
                ts=datetime.combine(day, time(0), ET),
                open=D(100),
                high=D(102),
                low=D(98),
                close=D(101),
                volume=D(2000000),
                source="alpaca",
            )
        )
    for day in [*_previous_sessions(now, 14), now.astimezone(ET).date()]:
        opening.append(
            Bar(
                symbol="AAPL",
                timeframe=Timeframe.M5,
                ts=datetime.combine(day, time(9, 30), ET),
                open=D(100),
                high=D(101),
                low=D(99),
                close=D("100.5"),
                volume=D(100000),
                source="alpaca",
            )
        )
    opening[-1] = opening[-1].model_copy(update={"volume": D(200000)})
    return daily, opening


def plan():
    d, o = evidence()
    result = form_plan("AAPL", d, o, now=NOW, feed="sip", version="orb@1.3.0")
    assert result.plan is not None
    return result.plan


def quote(bid="101.02", ask="101.04", ts=NOW):
    return Quote(symbol="AAPL", bid=D(bid), ask=D(ask), ts=ts, source="alpaca")


def test_measured_plan_has_no_score_or_manufactured_target():
    p = plan()
    assert p.relative_volume == 2
    assert p.daily_atr == 4
    assert p.trigger == D("101.01")
    assert p.stop == D("100.61")
    assert p.max_entry == D("101.41")
    assert p.exit_at.astimezone(ET).time() == time(15, 59)
    assert "target" not in p.model_fields
    assert "quality" not in p.model_fields
    assert evaluate_trigger(p, quote(), now=NOW).state == "BUY_ALLOWED"


@pytest.mark.parametrize(
    "bid,ask,reason",
    [
        ("101.00", "101.04", "ORB_WAITING_BREAKOUT"),  # ask alone cannot create a break
        ("101.41", "101.42", "ORB_ENTRY_MISSED"),
        ("102", "101", "ORB_QUOTE_INVALID"),
    ],
)
def test_explicit_price_boundaries(bid, ask, reason):
    assert evaluate_trigger(plan(), quote(bid, ask), now=NOW).reasons == [reason]


def test_approval_cannot_increase_the_limit_past_the_fixed_plan():
    assert evaluate_trigger(plan(), quote(), now=NOW, limit_price=D("101.42")).state == "NO_TRADE"
    assert (
        evaluate_trigger(plan(), quote(), now=NOW, limit_price=D("101.03")).state == "DATA_BLOCKED"
    )


@pytest.mark.parametrize("age", [6, -2])
def test_no_stale_or_future_quotes(age):
    assert (
        evaluate_trigger(plan(), quote(ts=NOW - timedelta(seconds=age)), now=NOW).state
        == "DATA_BLOCKED"
    )


def test_history_requires_same_opening_window_for_every_previous_session():
    d, o = evidence()
    assert form_plan("AAPL", d, o[1:], now=NOW, feed="sip").reasons == ["ORB_HISTORY_INCOMPLETE"]
    o[0] = o[0].model_copy(update={"ts": o[0].ts + timedelta(minutes=5)})
    assert form_plan("AAPL", d, o, now=NOW, feed="sip").reasons == ["ORB_HISTORY_INCOMPLETE"]


def test_feed_does_not_silently_change_the_relative_volume_definition():
    d, o = evidence()
    assert form_plan("AAPL", d, o, now=NOW, feed="otc").reasons == ["ORB_UNSUPPORTED_FEED"]


def test_current_daily_bar_cannot_leak_into_atr():
    d, o = evidence()
    d.append(d[-1].model_copy(update={"ts": NOW, "high": D(10000), "volume": D(999999999)}))
    assert form_plan("AAPL", d, o, now=NOW, feed="sip").plan.daily_atr == plan().daily_atr


def test_incomplete_opening_bar_cannot_publish_a_plan():
    d, o = evidence()
    result = form_plan("AAPL", d, o, now=NOW.replace(hour=13, minute=34), feed="sip")
    assert result.plan is None and result.state == "WAIT"


def test_conflicting_duplicate_bars_block_instead_of_choosing_one():
    d, o = evidence()
    o.append(o[-1].model_copy(update={"high": D(102)}))
    assert form_plan("AAPL", d, o, now=NOW, feed="sip").reasons == ["ORB_CONFLICTING_BARS"]


def test_plan_does_not_follow_the_price_down():
    p = plan()
    a = evaluate_trigger(p, quote("101.02", "101.04"), now=NOW)
    b = evaluate_trigger(p, quote("100.8", "100.9"), now=NOW + timedelta(seconds=1))
    assert a.plan == b.plan == p
    assert b.state == "WAIT"
    with pytest.raises(ValidationError):
        p.trigger = D(100)


def test_early_close_uses_the_actual_session_close():
    now = datetime(2026, 11, 27, 15, 0, tzinfo=UTC)
    d, o = evidence(now)
    p = form_plan("AAPL", d, o, now=now, feed="sip").plan
    assert p is not None and p.exit_at.astimezone(ET).time() == time(12, 59)
    assert evaluate_trigger(p, quote(ts=p.entry_deadline), now=p.entry_deadline).reasons == [
        "ORB_ENTRY_EXPIRED"
    ]


def test_bad_plan_geometry_cannot_be_deserialized():
    raw = plan().model_dump()
    raw["stop"] = raw["trigger"]
    with pytest.raises(ValidationError):
        OrbPlan.model_validate(raw)


def test_wider_paper_entry_accepts_new_band_but_not_above_limit():
    p = plan()
    assert evaluate_trigger(p, quote("101.20", "101.21"), now=NOW).state == "BUY_ALLOWED"
    assert evaluate_trigger(p, quote("101.40", "101.41"), now=NOW).state == "BUY_ALLOWED"
    assert evaluate_trigger(p, quote("101.41", "101.42"), now=NOW).state == "NO_TRADE"


def test_old_plan_keeps_its_original_limit_and_version():
    daily, opening = evidence()
    old = form_plan("AAPL", daily, opening, now=NOW, feed="sip", version="orb@1.1.0").plan
    assert old.version == "orb@1.1.0"
    assert old.max_entry == D("101.11")
    assert evaluate_trigger(old, quote("101.20", "101.21"), now=NOW).state == "NO_TRADE"
    assert evaluate_trigger(old, quote(), now=NOW).state == "BUY_ALLOWED"


def test_new_registry_version_preserves_registered_strict_parameters():
    from strategy.orb import LEGACY_PARAMETERS, PARAMETERS, VERSION
    from strategy.registry import ensure_builtin_strategies, register_version

    old = register_version(
        key="orb@1.1.0", name="orb", version_tag="1.1.0", parameters=LEGACY_PARAMETERS
    )
    versions = ensure_builtin_strategies()
    assert versions[0]["key"] == VERSION
    assert versions[0]["parameters"] == PARAMETERS
    unchanged = register_version(
        key="orb@1.1.0", name="orb", version_tag="1.1.0", parameters=LEGACY_PARAMETERS
    )
    assert unchanged["parameter_hash"] == old["parameter_hash"]


def test_early_paper_entry_is_lower_with_same_stop_and_chase_ceiling():
    d, o = evidence()
    p = form_plan("AAPL", d, o, now=NOW, feed="sip", version="orb@1.4.0").plan
    old = plan()
    assert p.trigger == D("100.81") < old.trigger
    assert p.stop == old.stop and p.max_entry == old.max_entry
    assert evaluate_trigger(p, quote("100.81", "100.83"), now=NOW).state == "BUY_ALLOWED"
    assert evaluate_trigger(old, quote("100.81", "100.83"), now=NOW).state == "WAIT"
    assert evaluate_trigger(p, quote("100.80", "100.83"), now=NOW).state == "WAIT"
    assert evaluate_trigger(p, quote("101.42", "101.44"), now=NOW).state == "NO_TRADE"
    assert evaluate_trigger(p, None, now=NOW).state == "DATA_BLOCKED"
    assert (
        evaluate_trigger(p, quote(ts=NOW - timedelta(seconds=6)), now=NOW).state == "DATA_BLOCKED"
    )


def test_pullback_policy_buys_below_reference_never_above_or_below_stop():
    d, o = evidence()
    p = form_plan("AAPL", d, o, now=NOW, feed="sip", version="orb@1.5.0").plan
    assert p.trigger == p.max_entry == D("100.81")
    assert p.stop == D("100.61")
    assert evaluate_trigger(p, quote("100.79", "100.81"), now=NOW).state == "BUY_ALLOWED"
    assert evaluate_trigger(p, quote("100.70", "100.72"), now=NOW).state == "BUY_ALLOWED"
    assert evaluate_trigger(p, quote("100.81", "100.82"), now=NOW).reasons == [
        "ORB_WAITING_PULLBACK"
    ]
    assert evaluate_trigger(p, quote("100.61", "100.63"), now=NOW).reasons == ["ORB_STOP_BREACHED"]
    assert (
        evaluate_trigger(p, quote("100.70", "100.72"), now=NOW, limit_price=D("100.82")).state
        != "BUY_ALLOWED"
    )
