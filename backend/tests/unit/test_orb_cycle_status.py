"""A closed exchange is a waiting state; a failed data service is an error."""

import pytest

from agents.scanner.cycle import run_cycle


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,reason,failed",
    [
        ("forming_range", "ORB_OUTSIDE_ENTRY_SESSION", False),
        ("forming_range", "ORB_OPENING_RANGE_FORMING", False),
        ("data_blocked", "ORB_SIP_ACCESS_DENIED", True),
    ],
)
async def test_cycle_distinguishes_waiting_from_failure(monkeypatch, status, reason, failed):
    from strategy.orb import runtime

    async def discover(*args, **kwargs):
        return {"status": status, "reason": reason}

    monkeypatch.setattr(runtime, "discover", discover)
    result = await run_cycle(context=object(), universe_service=object())
    assert result.error == (reason if failed else None)
