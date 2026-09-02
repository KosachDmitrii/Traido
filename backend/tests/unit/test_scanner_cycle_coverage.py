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
    make_symbol,
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
    monkeypatch.setattr(scanner, "is_kill_switch_on", lambda: False)
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


@pytest.mark.asyncio
async def test_a_cycle_stopped_by_a_full_queue_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare "0 proposals" must not be how a desk learns nothing was looked at."""
    cfg = _watchlist(["AAPL", "MSFT", "NVDA"])
    _install(monkeypatch, cfg, open_proposals=5)

    status = await scanner.run_scan_cycle()

    assert status.funnel.paused_on_full_queue is True
    assert status.funnel.deep_analysis_started == 0
    assert status.funnel.published == 0


@pytest.mark.asyncio
async def test_a_cycle_that_finished_the_universe_is_not_reported_as_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag must distinguish the two, so it cannot be always-on."""
    cfg = _watchlist(["AAPL", "MSFT", "NVDA"])
    seen = _install(monkeypatch, cfg, open_proposals=0)

    status = await scanner.run_scan_cycle()

    assert status.funnel.paused_on_full_queue is False
    assert sorted(seen) == ["AAPL", "MSFT", "NVDA"]


@pytest.mark.asyncio
async def test_the_pause_flag_reaches_the_desk_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """The desk reads the funnel as a dict; a flag it cannot see is no help."""
    cfg = _watchlist(["AAPL", "MSFT"])
    _install(monkeypatch, cfg, open_proposals=5)

    status = await scanner.run_scan_cycle()

    assert status.funnel.as_dict()["paused_on_full_queue"] is True


@pytest.mark.asyncio
async def test_the_whole_universe_is_covered_in_one_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tail is reached every cycle, not eventually.

    This is the property that replaced rotation. The old scanner capped a cycle
    at `max_symbols_per_cycle` because every name cost a full pipeline run, so
    the tail of a larger universe was only reachable by rotating the starting
    point across cycles — and a name was therefore looked at once every few
    cycles rather than every cycle. With the cheap stages in front, there is
    nothing to ration: all six are evaluated, every time.
    """
    symbols = ["A", "B", "C", "D", "E", "F"]
    cfg = _watchlist(symbols)
    _install(monkeypatch, cfg, open_proposals=0)

    status = await scanner.run_scan_cycle()

    assert status.funnel.universe_total == 6
    assert status.funnel.market_filter_evaluated == 6
    assert sorted(status.universe) == symbols


@pytest.mark.asyncio
async def test_a_universe_larger_than_the_old_cap_is_not_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two hundred names, and every one of them evaluated.

    The red-without-fix for the sixty-symbol limit: under the old
    `max_symbols_per_cycle` this cycle would have evaluated sixty.
    """
    symbols = [make_symbol(i) for i in range(200)]
    cfg = _watchlist(symbols)
    # Stage 3 is capped tightly here, which is the point rather than a
    # convenience: a two-hundred-name universe is supposed to reach the cheap
    # stages in full and the expensive one barely at all.
    narrow = scanner_settings(TRAIDO_QUANT_TOP_K=5, TRAIDO_DEEP_ANALYSIS_TOP_K=5)
    monkeypatch.setattr(scanner, "get_settings", lambda: narrow)
    monkeypatch.setattr(
        scan_cycle, "open_scan_context", lambda _s=None, **_kw: fake_scan_context(narrow)
    )
    _install(monkeypatch, cfg, open_proposals=0)

    status = await scanner.run_scan_cycle()

    assert status.funnel.universe_total == len(symbols) > 60
    assert status.funnel.market_filter_evaluated == len(symbols)
    assert status.funnel.deep_analysis_started == 5, "expensive work must stay bounded"


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


@pytest.mark.asyncio
async def test_a_paused_cycle_comes_back_sooner_than_a_real_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Doing nothing must not cost the same as scanning the whole universe.

    This is what the operator actually experiences: clearing the queue looks
    like it did nothing, because the scanner is still sleeping off an interval
    it earned by examining zero symbols.
    """
    cfg = _watchlist(["A", "B"])
    cfg["scan_interval_seconds"] = 300
    _install(monkeypatch, cfg, open_proposals=5)

    waits = await _waits_of_one_cycle(monkeypatch)

    assert waits == [scanner.PAUSED_RETRY_SECONDS]


@pytest.mark.asyncio
async def test_a_productive_cycle_waits_for_its_next_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wait is what remains of the cadence, not a fresh full interval.

    The difference is the whole point of scheduling: sleeping a full interval
    *after* finishing turns a four-minute cycle and a five-minute interval into
    a nine-minute period. Here a cycle that took almost no time leaves almost
    the whole interval, and a cycle that took two minutes would leave three.

    Only when there is already something to confirm — otherwise the desk hunts.
    """
    cfg = _watchlist(["A", "B"])
    cfg["scan_interval_seconds"] = 300
    _install(monkeypatch, cfg, open_proposals=1)

    waits = await _waits_of_one_cycle(monkeypatch)

    assert len(waits) == 1
    assert 295.0 < waits[0] <= 300.0


@pytest.mark.asyncio
async def test_empty_buy_queue_hunts_sooner_than_the_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No open BUY → keep scanning. WAIT watches do not earn a full nap."""
    monkeypatch.setattr(
        "market_data.providers.alpaca.market_data_cooldown_seconds",
        lambda: 0.0,
    )
    cfg = _watchlist(["A", "B"])
    cfg["scan_interval_seconds"] = 300
    _install(monkeypatch, cfg, open_proposals=0)

    waits = await _waits_of_one_cycle(monkeypatch)

    assert waits == [scanner.HUNTING_RETRY_SECONDS]


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
async def test_the_retry_never_outlasts_the_configured_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A desk configured to scan every 30 s must not wait longer when paused."""
    cfg = _watchlist(["A", "B"])
    cfg["scan_interval_seconds"] = 30
    monkeypatch.setattr(scanner, "PAUSED_RETRY_SECONDS", 120)
    _install(monkeypatch, cfg, open_proposals=5)

    waits = await _waits_of_one_cycle(monkeypatch)

    assert waits == [30]


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


@pytest.mark.asyncio
async def test_a_wake_during_a_cycle_is_not_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clearing the flag after the cycle would swallow it and stall for an interval.

    A proposal decided while a scan is in flight is exactly when this matters:
    the room appears before the cycle ends, so the flag has to survive into the
    wait that follows.
    """
    cfg = _watchlist(["A", "B"])
    cfg["scan_interval_seconds"] = 300
    _install(monkeypatch, cfg, open_proposals=0)

    async def _pipeline(symbol: str, **_kwargs: object) -> SimpleNamespace:
        scanner.wake_scanner()
        return _empty_result()

    monkeypatch.setattr(scan_cycle, "run_symbol_pipeline", _pipeline)

    pending: list[bool] = []
    reached = asyncio.Event()

    async def _record(_delay: float) -> None:
        pending.append(scanner._wake_token != scanner._wake_seen)
        reached.set()
        await asyncio.sleep(3600)

    monkeypatch.setattr(scanner, "wait_before_next_cycle", _record)
    task = asyncio.create_task(scanner.scanner_loop())
    try:
        await asyncio.wait_for(reached.wait(), timeout=2.0)
    finally:
        task.cancel()

    assert pending == [True], "a wake raised mid-cycle was discarded before the wait"
