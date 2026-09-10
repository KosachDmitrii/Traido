"""The scanner must cover its universe, tell the truth about why a cycle
stopped, and not sleep off a full interval after doing nothing.

All three failures are silent by nature. A cycle that stops on a full proposal
queue reports the same "0 proposals" as one that scanned everything and found
nothing; a cycle that only ever looks at a prefix of the universe never reaches
the tail; and a scanner that paces empty cycles like real ones leaves the desk
idle for minutes after the operator has cleared the very thing that stopped it.
None of them shows up as an error.

The same silence applies when the confirm queue is empty and only WAIT watches
remain: sleeping the configured interval then is pacing a hunt like a nap on a
full desk. Empty BUY queue → short retry; open BUY → configured cadence.

The coverage tests here changed shape with the staged funnel. There is no
per-cycle symbol cap and no rotation cursor any more — the cheap stages look at
the whole universe every cycle, and cost is controlled by how few names reach
the expensive ones. So the property to protect is no longer "rotation eventually
reaches the tail", it is the stronger "the tail is reached on every cycle".
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agents.scanner import agent as scanner
from agents.scanner import cycle as scan_cycle
from tests.scanner_fakes import (
    fake_scan_context,
    scanner_settings,
    universe_service_for,
)


@pytest.fixture(autouse=True)
def quiet_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run cycles without touching the activity board, config, or the clock."""
    settings = scanner_settings()
    monkeypatch.setattr(scanner.BOARD, "log", lambda *a, **k: None)
    monkeypatch.setattr(scanner.BOARD, "set_agent", lambda *a, **k: None)
    monkeypatch.setattr(scan_cycle.BOARD, "log", lambda *a, **k: None)
    monkeypatch.setattr(scan_cycle.BOARD, "set_agent", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "get_settings", lambda: settings)
    monkeypatch.setattr(
        scan_cycle, "open_scan_context", lambda _s=None, **_kw: fake_scan_context(settings)
    )
    monkeypatch.setattr(scanner, "_wake_token", 0)
    monkeypatch.setattr(scanner, "_wake_seen", 0)
    monkeypatch.setattr(scanner, "_schedule", None)
    monkeypatch.setattr(scanner, "WAKE_POLL_SECONDS", 0.01)
    scanner.STATUS.cycle = 0
    scanner.STATUS.error = None
    scanner.STATUS.deep_symbols = []
    scanner.STATUS.previous_deep_symbols = []
    scanner.STATUS.deep_unique_new = 0
    scanner.STATUS.deep_overlap = 0
    scanner.STATUS.deep_uniqueness_ratio = 0.0
    from agents.scanner.funnel import ScanFunnel

    scanner.STATUS.funnel = ScanFunnel()


def _watchlist(symbols: list[str], *, max_open: int = 5) -> dict:
    return {
        "universe": symbols,
        "timeframes": ["1d"],
        "max_open_buy_opportunities": max_open,
        "enabled": True,
    }


def _empty_result() -> SimpleNamespace:
    """A symbol that produced no candidate — the common, uninteresting case."""
    return SimpleNamespace(status="no_candidate", candidate=None, risk=None, opportunity=None)


def _fake_open(n: int) -> list:
    """Stand-ins for open BUY cards. Must expose `.candidate.symbol` — the cycle
    reads that set to skip duplicates, and a bare `None` blows up mid-scan."""
    return [SimpleNamespace(candidate=SimpleNamespace(symbol=f"OPEN{i}")) for i in range(n)]


def _install(
    monkeypatch: pytest.MonkeyPatch,
    cfg: dict,
    *,
    open_proposals: int = 0,
) -> list[str]:
    """Wire a cycle up and return the list that records what reached Stage 3."""
    seen: list[str] = []
    symbols = list(cfg["universe"])
    open_cards = _fake_open(open_proposals)

    monkeypatch.setattr(scanner, "load_watchlist", lambda: cfg)
    monkeypatch.setattr(scanner, "universe_service", lambda _s=None: universe_service_for(symbols))
    monkeypatch.setattr(scan_cycle.OPPORTUNITIES, "list_open", lambda: list(open_cards))
    monkeypatch.setattr(scanner, "open_buy_count", lambda: open_proposals)
    # These tests ask which symbols a cycle covered. Queue hygiene is a separate
    # question, tested on its own in `test_stale_proposals_are_taken_down.py`.
    monkeypatch.setattr(scan_cycle, "withdraw_unactionable", lambda *a, **k: 0)

    async def _pipeline(symbol: str, **_kwargs: object) -> SimpleNamespace:
        seen.append(symbol)
        return _empty_result()

    monkeypatch.setattr(scan_cycle, "run_symbol_pipeline", _pipeline)
    return seen


def test_deep_rotation_is_measured_between_adjacent_cycles() -> None:
    from agents.scanner.cycle import CycleResult

    first = CycleResult(deep_symbols=["A", "B", "C", "D"])
    scanner._absorb(first)
    assert scanner.STATUS.deep_unique_new == 4
    assert scanner.STATUS.deep_overlap == 0
    assert scanner.STATUS.deep_uniqueness_ratio == 1.0

    second = CycleResult(deep_symbols=["C", "D", "E", "F"])
    scanner._absorb(second)

    assert scanner.STATUS.previous_deep_symbols == ["A", "B", "C", "D"]
    assert scanner.STATUS.deep_symbols == ["C", "D", "E", "F"]
    assert scanner.STATUS.deep_unique_new == 2
    assert scanner.STATUS.deep_overlap == 2
    assert scanner.STATUS.deep_uniqueness_ratio == 0.5


async def _waits_of_one_cycle(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Run `scanner_loop` for one cycle and report the delay it chose.

    The loop never returns, so it is cancelled as soon as it reaches its first
    wait — which is the thing under test.
    """
    waits: list[float] = []
    reached = asyncio.Event()

    async def _record(delay: float) -> None:
        waits.append(delay)
        reached.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(scanner, "wait_before_next_cycle", _record)
    task = asyncio.create_task(scanner.scanner_loop())
    try:
        await asyncio.wait_for(reached.wait(), timeout=2.0)
    finally:
        task.cancel()
    return waits


def test_provider_failure_cools_down_instead_of_hunting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 429 cycle waits for the vendor window, not a fixed hunt nap."""
    monkeypatch.setattr(
        "market_data.providers.alpaca.market_data_cooldown_seconds",
        lambda: 45.0,
    )
    delay = scanner.choose_scan_delay(
        paused_on_full_queue=False,
        open_buys=0,
        interval=300.0,
        seconds_until_due=300.0,
        provider_failed=True,
    )
    assert delay == 45.0


def test_cycle_provider_failed_reads_funnel_and_error() -> None:
    from agents.scanner.funnel import ScanFunnel

    ok = scanner.ScannerStatus()
    assert scanner.cycle_provider_failed(ok) is False

    hurt = scanner.ScannerStatus(funnel=ScanFunnel(provider_failed=12))
    assert scanner.cycle_provider_failed(hurt) is True

    errored = scanner.ScannerStatus(error="snapshot_batch_failed: HTTPStatusError(429)")
    assert scanner.cycle_provider_failed(errored) is True


@pytest.mark.asyncio
async def test_waking_the_scanner_ends_the_wait() -> None:
    """Deciding a proposal is the desk saying "there is room now"."""
    waiting = asyncio.create_task(scanner.wait_before_next_cycle(3600))
    await asyncio.sleep(0.05)
    assert not waiting.done(), "the wait ended before anything asked it to"

    scanner.wake_scanner()

    await asyncio.wait_for(waiting, timeout=1.0)


@pytest.mark.asyncio
async def test_the_wait_still_expires_on_its_own() -> None:
    """The wake is a shortcut, not the only way out; the timer must still fire."""
    await asyncio.wait_for(scanner.wait_before_next_cycle(0.02), timeout=1.0)


@pytest.mark.asyncio
async def test_the_wake_survives_a_second_event_loop() -> None:
    """The scanner outlives any one loop; tests and restarts both make new ones.

    An `asyncio.Event` at module scope would bind to whichever loop first
    awaited it and reject the rest — a failure that only appears on the second
    app start in a process, which is the worst place to find it.
    """
    scanner.wake_scanner()

    await asyncio.wait_for(scanner.wait_before_next_cycle(3600), timeout=1.0)
