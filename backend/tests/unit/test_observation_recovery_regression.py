"""Failure ownership and freshness scheduling; no broker or production data."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.scanner.agent import cap_observation_delay
from strategy.orb import runtime
from strategy.orb.observation_status import next_observation_at
from tests.unit.test_orb_retest import scenario


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["session", "history", "cancel", "projection"])
@pytest.mark.parametrize("correction", [False, True])
async def test_failed_stream_batch_returns_to_queue_and_preserves_newer_correction(
    monkeypatch, failure, correction
):
    plan, rows, now = scenario()
    bar = rows[0].model_copy(update={"close": plan.range_high})
    corrected = bar.model_copy(update={"volume": bar.volume + 1})
    saved = {
        "session": plan.session,
        "plans": {plan.symbol: plan.model_dump(mode="json")},
        "states": {plan.symbol: {"state": "WAIT", "reasons": ["ORB_RETEST_WAIT_BREAKOUT"]}},
    }

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz or UTC)

    monkeypatch.setattr(runtime, "datetime", Clock)
    monkeypatch.setattr(runtime, "_pending_completed_bars", {})
    monkeypatch.setattr(runtime, "_last_ready_check", 0)
    runtime.notify_completed_bar(bar)

    def fail(*args, **kwargs):
        if correction:
            runtime.notify_completed_bar(corrected)
        if failure == "cancel":
            raise asyncio.CancelledError
        raise RuntimeError("simulated database outage")

    monkeypatch.setattr(
        runtime, "read_session", fail if failure == "session" else lambda day: saved
    )
    monkeypatch.setattr(
        "strategy.orb.retest_data.history_complete",
        fail if failure in {"history", "cancel"} else lambda *a, **k: True,
    )
    monkeypatch.setattr(
        runtime, "update_states", fail if failure == "projection" else lambda *a: None
    )
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
        await runtime.observe_priority(SimpleNamespace())
    expected = corrected if correction else bar
    assert runtime._pending_completed_bars[(plan.symbol, bar.ts)] == expected
    assert not runtime._priority_lock.locked()
    # Restart the pass after recovery. It consumes the same captured bar once.
    monkeypatch.setattr(runtime, "read_session", lambda day: saved)
    monkeypatch.setattr("strategy.orb.retest_data.history_complete", lambda *a, **k: True)
    written = []
    monkeypatch.setattr(runtime, "update_states", lambda day, updates: written.append(updates))
    assert await runtime.observe_priority(SimpleNamespace()) == {"wait_for_entry": 1}
    assert runtime._pending_completed_bars == {}
    assert len(written) == 1
    assert written[0][plan.symbol]["last_bar"]["volume"] == str(expected.volume)


def test_scan_starts_before_oldest_check_expires_even_with_open_buy():
    now = datetime(2026, 10, 9, 14, tzinfo=UTC)
    session = {
        "plans": {"OLD": {}, "NEW": {}},
        "states": {
            "OLD": {"last_checked_at": (now - timedelta(seconds=250)).isoformat()},
            "NEW": {"last_checked_at": now.isoformat()},
        },
    }
    due = next_observation_at(session, now=now, reserve_seconds=30)
    assert due == now + timedelta(seconds=20)
    assert (
        cap_observation_delay(
            293, {"pending": 0, "next_check_at": due.isoformat()}, now=now, cooldown=0
        )
        == 20
    )
    # Vendor cooldown is never bypassed for freshness.
    assert (
        cap_observation_delay(
            293, {"pending": 0, "next_check_at": due.isoformat()}, now=now, cooldown=60
        )
        == 60
    )


@pytest.mark.parametrize(
    "stamp", [None, "invalid", "2026-10-09T14:00:00", "2026-10-10T14:00:00+00:00"]
)
def test_missing_invalid_or_future_stamps_require_observation(stamp):
    now = datetime(2026, 10, 9, 14, tzinfo=UTC)
    session = {"plans": {"A": {}}, "states": {"A": {"last_checked_at": stamp}}}
    assert next_observation_at(session, now=now, reserve_seconds=30) == now
    assert cap_observation_delay(300, {"next_check_at": now.isoformat()}, now=now, cooldown=0) == 5


def test_empty_universe_and_pending_portions_preserve_existing_pacing():
    now = datetime.now(UTC)
    assert next_observation_at({}, now=now, reserve_seconds=30) is None
    assert cap_observation_delay(300, {}, now=now, cooldown=0) == 300
    assert cap_observation_delay(300, {"pending": 1}, now=now, cooldown=45) == 45


@pytest.mark.asyncio
@pytest.mark.parametrize("rth,cooldown,expected", [(True, 0, 20), (True, 60, 60), (False, 0, 293)])
async def test_real_scanner_loop_caps_wait_without_changing_closed_market_pacing(
    monkeypatch, rth, cooldown, expected
):
    from agents.scanner import agent as scanner

    now = datetime(2026, 10, 9, 14, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz or UTC)

    monkeypatch.setattr(scanner, "datetime", Clock)
    monkeypatch.setattr(scanner, "load_watchlist", lambda: {"scan_interval_seconds": 300})
    schedule = SimpleNamespace(
        retarget=lambda value: None,
        begin=lambda: 0,
        complete=lambda: 0,
        as_dict=dict,
        seconds_until_due=lambda: 293,
    )
    monkeypatch.setattr(scanner, "_schedule", schedule)
    monkeypatch.setattr(scanner, "open_buy_count", lambda: 1)
    status = SimpleNamespace(funnel=SimpleNamespace(paused_on_full_queue=False))
    monkeypatch.setattr(scanner, "run_scan_cycle", AsyncMock(return_value=status))
    monkeypatch.setattr(scanner, "cycle_provider_failed", lambda value: False)
    monkeypatch.setattr("trading.session_hours.us_equity_rth_open", lambda now: rth)
    monkeypatch.setattr(
        "market_data.providers.alpaca.market_data_cooldown_seconds", lambda: cooldown
    )
    monkeypatch.setattr(
        runtime,
        "OBSERVATION",
        {"pending": 0, "next_check_at": (now + timedelta(seconds=20)).isoformat()},
    )
    waits = []

    async def wait(delay):
        waits.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(scanner, "wait_before_next_cycle", wait)
    with pytest.raises(asyncio.CancelledError):
        await scanner._scanner_loop_inner()
    assert waits == [expected]
