"""One walker over the universe, always.

There is exactly one `STATUS` and one `STATUS.funnel`. Two passes running at once
do not produce two reports — they interleave into one incoherent report and one
doubled market-data request rate. This was live behaviour, not a hypothetical:
`POST /scanner/run` ran a cycle inline while the scanner loop was already running
one, and the desk requested it on every page load. The logs showed
`scanned 191/60` against a universe of 60, a summary line claiming `scanned 0`
and `outranked 5` at once, the same symbol analysed four times in twenty seconds,
and 175 `429 Too Many Requests` in one cycle.
"""

from __future__ import annotations

import asyncio

import pytest

from agents.scanner import agent as scanner
from agents.scanner import cycle as scan_cycle
from tests.scanner_fakes import fake_scan_context, scanner_settings, universe_service_for


@pytest.fixture(autouse=True)
def quiet_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A universe of three symbols and a pipeline that yields but does nothing."""
    monkeypatch.setattr(
        scanner,
        "load_watchlist",
        lambda: {
            "universe": ["AAA", "BBB", "CCC"],
            "timeframes": ["1d"],
            "max_open_buy_opportunities": 5,
            "enabled": True,
        },
    )
    settings = scanner_settings()
    monkeypatch.setattr(scanner, "is_kill_switch_on", lambda: False)
    monkeypatch.setattr(scanner, "get_settings", lambda: settings)
    monkeypatch.setattr(
        scanner, "universe_service", lambda _s=None: universe_service_for(["AAA", "BBB", "CCC"])
    )
    monkeypatch.setattr(
        scan_cycle, "open_scan_context", lambda _s=None, **_kw: fake_scan_context(settings)
    )
    monkeypatch.setattr(scan_cycle.OPPORTUNITIES, "list_open", list)
    monkeypatch.setattr(scan_cycle, "withdraw_unactionable", lambda *a, **k: 0)
    monkeypatch.setattr(scanner.STATUS, "cycle", 0)


def _install_pipeline(monkeypatch: pytest.MonkeyPatch, seen: list[str]) -> None:
    async def pipeline(symbol: str, **_: object):
        seen.append(symbol)
        # A real pipeline awaits network calls; without a suspension point here
        # a second cycle could never interleave and the test would pass for the
        # wrong reason.
        await asyncio.sleep(0)

        class _Result:
            status = "no_candidate"
            candidate = None
            risk = None

        return _Result()

    monkeypatch.setattr(scan_cycle, "run_symbol_pipeline", pipeline)


@pytest.mark.asyncio
async def test_a_second_cycle_launched_mid_pass_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    _install_pipeline(monkeypatch, seen)

    await asyncio.gather(scanner.run_scan_cycle(), scanner.run_scan_cycle())

    assert sorted(seen) == ["AAA", "BBB", "CCC"], f"the universe was walked more than once: {seen}"


@pytest.mark.asyncio
async def test_the_funnel_describes_one_pass_not_several(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More analyses than the universe holds is the signature of the original bug."""
    seen: list[str] = []
    _install_pipeline(monkeypatch, seen)

    await asyncio.gather(*(scanner.run_scan_cycle() for _ in range(4)))

    funnel = scanner.STATUS.funnel
    assert funnel.universe_total == 3
    assert funnel.deep_analysis_started == 3, (
        f"the funnel counted {funnel.deep_analysis_started} analyses out of 3"
    )
    assert funnel.reconciles(), "four interleaved passes left the ledger unbalanced"


@pytest.mark.asyncio
async def test_the_cycle_counter_advances_once_per_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Refused callers must not consume a cycle number either, or the log shows
    several different reports under one heading."""
    _install_pipeline(monkeypatch, [])

    await asyncio.gather(*(scanner.run_scan_cycle() for _ in range(3)))
    assert scanner.STATUS.cycle == 1


@pytest.mark.asyncio
async def test_the_guard_is_released_when_a_pass_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cycle that dies must not lock the scanner out for the process lifetime."""

    async def exploding(symbol: str, **_: object):
        raise RuntimeError("market data down")

    monkeypatch.setattr(scan_cycle, "run_symbol_pipeline", exploding)
    await scanner.run_scan_cycle()

    seen: list[str] = []
    _install_pipeline(monkeypatch, seen)
    await scanner.run_scan_cycle()
    assert sorted(seen) == ["AAA", "BBB", "CCC"], "the scanner never ran again after a failed cycle"


@pytest.mark.asyncio
async def test_request_rescan_aborts_the_in_flight_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changing entry policy mid-pass must not wait for the old thresholds to finish."""
    started = asyncio.Event()
    release = asyncio.Event()
    seen: list[str] = []

    async def slow_pipeline(symbol: str, **_: object):
        seen.append(symbol)
        started.set()
        await release.wait()

        class _Result:
            status = "no_candidate"
            candidate = None
            risk = None

        return _Result()

    monkeypatch.setattr(scan_cycle, "run_symbol_pipeline", slow_pipeline)

    walker = asyncio.create_task(scanner.run_scan_cycle())
    await asyncio.wait_for(started.wait(), timeout=2.0)
    assert scanner.abort_scan_cycle() is True
    status = await asyncio.wait_for(walker, timeout=2.0)

    assert status.error == "superseded"
    assert scanner.STATUS.running is False
    assert seen, "the cycle must have started before it was aborted"
    # In-flight symbol tasks may have begun; none should still be blocked on release.
    release.set()

    seen.clear()
    _install_pipeline(monkeypatch, seen)
    await scanner.run_scan_cycle()
    assert sorted(seen) == ["AAA", "BBB", "CCC"]
