"""Auto Scanner — watches a universe; user never picks symbols manually.

This module owns the scanner's *lifecycle*: configuration, the single-walker
guard, status for the desk, and the cadence between cycles. The cycle itself —
the staged funnel from universe down to published proposals — is
`agents.scanner.cycle`, kept apart because the two change for different reasons.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from agents.scanner.cycle import CycleResult, rank_key, run_cycle
from agents.scanner.funnel import ScanFunnel
from agents.scanner.schedule import ScanSchedule
from core.activity import BOARD
from core.config import Settings, get_settings
from core.enums import Timeframe
from core.universe import universe_from_watchlist
from risk.kill_switch import is_kill_switch_on
from universe.eligibility import EligibilityPolicy
from universe.provider import StaticUniverseProvider, create_universe_provider
from universe.service import UniverseService

__all__ = [
    "STATUS",
    "ScanFunnel",
    "ScannerStatus",
    "all_symbols",
    "load_watchlist",
    "rank_key",
    "resolve_universe",
    "run_scan_cycle",
    "scanner_loop",
    "start_scanner",
    "stop_scanner",
    "universe_service",
    "wake_scanner",
]

WATCHLIST_PATH = Path(__file__).resolve().parents[2] / "configs" / "watchlist.json"

FALLBACK_WATCHLIST = {
    "universe": ["AAPL", "MSFT", "NVDA"],
    "timeframes": ["1d", "1h"],
    "scan_interval_seconds": 300,
    "max_open_buy_opportunities": 5,
    "enabled": True,
}

SCAN_PACING_SECONDS = 0.4
"""Gap between symbols, to stay inside the market-data provider's rate limit."""

PAUSED_RETRY_SECONDS = 30
"""How soon to come back after a cycle that never got to scan anything.

A cycle halted by a full proposal queue did no work, so the interval that paces
real scanning is the wrong wait: it charges a full cycle's delay for having
looked at nothing, and the universe stays unscanned long after the queue
cleared.
"""

HUNTING_RETRY_SECONDS = 30
"""How soon to come back when there is nothing to confirm.

Open BUY cards are the only proposals the operator can act on. WAIT watches are
plans, not proposals — sleeping a full interval while the confirm queue is empty
leaves the desk staring at waits with nothing to approve. Hunt until a BUY
appears; then return to the configured cadence.
"""


def open_buy_count() -> int:
    """Cards awaiting confirmation — not WAIT watches."""
    from trading.opportunities import OPPORTUNITIES

    return len(OPPORTUNITIES.list_open())


def choose_scan_delay(
    *,
    paused_on_full_queue: bool,
    open_buys: int,
    interval: float,
    seconds_until_due: float,
) -> float:
    """Pick the wait after a cycle. Empty confirm queue hunts; full queue retries."""
    if paused_on_full_queue:
        return min(PAUSED_RETRY_SECONDS, interval)
    if open_buys <= 0:
        return min(HUNTING_RETRY_SECONDS, interval)
    return max(0.0, seconds_until_due)


@dataclass
class ScannerStatus:
    enabled: bool = True
    running: bool = False
    last_started_at: str | None = None
    last_finished_at: str | None = None
    last_symbol: str | None = None
    symbols_scanned: int = 0
    """How many names reached deep analysis.

    Renamed in meaning, not in name: it used to be the whole per-cycle list,
    because the whole list was analysed. Now it is the finalists, which is the
    number that actually costs anything. `funnel.universe_total` is the number
    the desk looked at.
    """

    opportunities_found: int = 0
    cycle: int = 0
    error: str | None = None
    universe: list[str] = field(default_factory=list)
    funnel: ScanFunnel = field(default_factory=ScanFunnel)
    stage_seconds: dict[str, float] = field(default_factory=dict)
    provider_stats: dict[str, dict[str, float]] = field(default_factory=dict)
    ai_budget: dict[str, float | int] = field(default_factory=dict)
    schedule: dict[str, float | int | None] = field(default_factory=dict)
    shortlist: list[str] = field(default_factory=list)


STATUS = ScannerStatus()
_task: asyncio.Task | None = None
_schedule: ScanSchedule | None = None

_universe_service: UniverseService | None = None
_wake_token = 0
"""Bumped whenever someone wants a cycle sooner than the timer would give one.

A counter rather than an `asyncio.Event` on purpose: a module-level Event binds
itself to the first loop that awaits it and then rejects every other one, and
this module outlives individual loops — tests and app restarts each make a new
one. An integer has no such opinion.
"""

_wake_seen = 0
"""The token the current cycle started from. Anything newer is a pending wake."""

WAKE_POLL_SECONDS = 1.0
"""How closely `wait_before_next_cycle` watches for a wake. Latency, not logic."""


def wake_scanner() -> None:
    """Cut short the wait before the next cycle.

    A cycle that paused on a full queue is waiting for exactly one thing —
    room — and the desk knows the instant it appears. Left to its own timer the
    scanner finds out minutes later, so deciding a proposal appears to do
    nothing at all.

    `PAUSED_RETRY_SECONDS` is what keeps this an optimisation rather than a
    correctness requirement: a call site that forgets to wake the scanner costs
    seconds, not a full interval.
    """
    global _wake_token
    _wake_token += 1


def load_watchlist() -> dict:
    if not WATCHLIST_PATH.exists():
        return dict(FALLBACK_WATCHLIST)
    try:
        return json.loads(WATCHLIST_PATH.read_text())
    except json.JSONDecodeError:
        # A malformed watchlist must not take the desk down; fall back and say so.
        BOARD.log("scanner", "watchlist.json is invalid — using fallback universe", level="error")
        return dict(FALLBACK_WATCHLIST)


def all_symbols(cfg: dict) -> list[str]:
    """The configured universe, before any per-cycle capping."""
    try:
        return universe_from_watchlist(cfg).symbols
    except KeyError as exc:
        BOARD.log("scanner", f"Universe config error: {exc}", level="error")
        return list(FALLBACK_WATCHLIST["universe"])


def resolve_universe(cfg: dict, *, offset: int = 0) -> list[str]:
    """The curated watchlist symbols.

    Kept because the settings and evaluation pages describe the *curated* list,
    which is still a real thing — it is the CORE tier and the source of the
    sector map the risk engine's correlation clustering uses.

    It is no longer what a cycle scans. There is no per-cycle cap and no
    rotation cursor any more: the funnel decides how many names reach expensive
    work, so a cap on how many are *looked at* would only hide part of the
    market from the cheap stages that exist to search it.
    """
    del offset
    return all_symbols(cfg)


def universe_service(settings: Settings | None = None) -> UniverseService:
    """The desk's universe, built once and cached across cycles.

    A module singleton because the reference snapshot behind it is the thing
    being cached; rebuilding the service per cycle would throw the cache away
    and re-download fourteen thousand asset records every five minutes.
    """
    global _universe_service
    if _universe_service is None:
        resolved = settings or get_settings()
        _universe_service = UniverseService(
            create_universe_provider(resolved),
            curated_provider=StaticUniverseProvider(),
            policy=EligibilityPolicy(),
            refresh_sec=resolved.universe_refresh_seconds,
        )
    return _universe_service


def reset_universe_service() -> None:
    """Drop the cached service. For tests and for a configuration change."""
    global _universe_service
    _universe_service = None


_cycle_active = False
"""True while a pass is walking the universe.

There is exactly one `STATUS` and one `STATUS.funnel`, so two passes running at
once do not produce two reports — they interleave into one incoherent report and
one doubled request rate. Observed live: `scanned 191/60` against a universe of
60, a cycle summary claiming `scanned 0` and `outranked 5` in the same line, the
same symbol analysed four times in twenty seconds, and 175 `429 Too Many
Requests` in a single cycle, because `SCAN_PACING_SECONDS` paces one walker and
says nothing about how many walkers there are.

A plain flag rather than a lock: asyncio gives us no preemption between the read
and the write below, and a module-level `asyncio.Lock` would carry the
loop-affinity problem described on `_wake_token`.
"""


async def run_scan_cycle() -> ScannerStatus:
    """Walk the universe once, or decline if a walk is already under way.

    Declining rather than queueing is deliberate: the caller wanted a fresh view
    of the universe and one is already being produced.
    """
    global _cycle_active
    if _cycle_active:
        BOARD.log(
            "scanner",
            f"Cycle {STATUS.cycle} already running — request ignored",
            level="warn",
        )
        return STATUS
    _cycle_active = True
    try:
        return await _scan_once()
    finally:
        _cycle_active = False


async def _scan_once() -> ScannerStatus:
    cfg = load_watchlist()
    settings = get_settings()
    STATUS.enabled = bool(cfg.get("enabled", True))
    if not STATUS.enabled or is_kill_switch_on():
        STATUS.running = False
        STATUS.error = "disabled_or_kill_switch"
        BOARD.set_agent("scanner", status="idle", detail=STATUS.error)
        return STATUS

    tfs = tuple(Timeframe(t) for t in (cfg.get("timeframes") or ["1d", "1h"]))
    max_open = int(cfg.get("max_open_buy_opportunities") or 5)
    STATUS.running = True
    STATUS.last_started_at = datetime.now(UTC).isoformat()
    STATUS.error = None
    STATUS.cycle += 1
    BOARD.set_agent(
        "scanner",
        status="working",
        detail=f"Cycle {STATUS.cycle} \u00b7 universe {settings.universe_mode.value}",
    )
    BOARD.log(
        "scanner", f"Cycle {STATUS.cycle} started \u00b7 {settings.universe_mode.value} universe"
    )

    result: CycleResult | None = None
    try:
        result = await run_cycle(
            settings=settings,
            universe_service=universe_service(settings),
            timeframes=tfs,
            max_open=max_open,
            scheduled_at=datetime.now(UTC),
        )
    except Exception as exc:  # noqa: BLE001
        STATUS.error = str(exc)
        BOARD.set_agent("scanner", status="error", detail=str(exc)[:80])
        BOARD.log("scanner", f"Cycle failed: {exc}", level="error")
    finally:
        STATUS.running = False
        STATUS.last_finished_at = datetime.now(UTC).isoformat()
        if result is not None:
            _absorb(result)
        if not STATUS.error:
            BOARD.set_agent("scanner", status="idle", detail=_cycle_detail())
            BOARD.log("scanner", _funnel_summary(STATUS.funnel, STATUS.cycle))
            top = STATUS.funnel.top_rejections()
            if top:
                detail = ", ".join(f"{reason} x{count}" for reason, count in top)
                BOARD.log("scanner", f"Top risk rejections: {detail}", level="warn")
        from core.desk_bus import DESK_BUS

        DESK_BUS.bump_desk(
            kind="scan_cycle",
            cycle=STATUS.cycle,
            found=STATUS.funnel.published,
        )
    return STATUS


def _absorb(result: CycleResult) -> None:
    """Copy one cycle's report onto the status the desk reads."""
    STATUS.funnel = result.funnel
    STATUS.universe = result.universe_symbols
    STATUS.shortlist = result.shortlist
    STATUS.symbols_scanned = result.funnel.deep_analysis_started
    STATUS.opportunities_found = result.funnel.published
    STATUS.stage_seconds = result.timings.as_dict()
    STATUS.provider_stats = result.provider_stats
    STATUS.ai_budget = result.ai_budget
    STATUS.last_symbol = result.shortlist[-1] if result.shortlist else None
    if result.error and not STATUS.error:
        STATUS.error = result.error
    _record_metrics(result)


def _record_metrics(result: CycleResult) -> None:
    """Publish the cycle's numbers where they outlive the cycle.

    `STATUS` holds the last cycle only, which answers "what is it doing" and not
    "has it been getting slower since Tuesday". Labels here are stage names,
    result classes and resource names — all closed sets. No symbol is ever a
    label; that is what turns a metrics endpoint into a memory leak.
    """
    from core.metrics import METRICS

    funnel = result.funnel
    METRICS.gauge(
        "traido_universe_size",
        funnel.universe_total,
        help_text="Instruments offered by the universe provider, before filters.",
    )
    METRICS.gauge("traido_eligible_universe_size", funnel.structurally_eligible)
    METRICS.gauge("traido_quant_candidates", funnel.quant_shortlisted)
    METRICS.gauge("traido_deep_analysis_candidates", funnel.deep_analysis_started)
    METRICS.gauge("traido_published_opportunities", funnel.published)

    for stage, seconds in result.timings.as_dict().items():
        if stage != "total":
            METRICS.gauge("traido_scan_stage_duration_seconds", seconds, labels={"stage": stage})
    METRICS.observe(
        "traido_scan_duration_seconds",
        result.timings.total,
        help_text="Wall-clock seconds for one complete scan cycle.",
    )

    for stage, outcome, count in (
        ("stage0", "rejected", funnel.stage0_rejected),
        ("stage1", "passed", funnel.market_filter_passed),
        ("stage1", "rejected", funnel.market_filter_rejected),
        ("stage2", "shortlisted", funnel.quant_shortlisted),
        ("stage2", "rejected", funnel.quant_rejected),
        ("stage2", "outranked", funnel.quant_outranked),
        ("stage3", "started", funnel.deep_analysis_started),
        ("stage3", "failed", funnel.deep_analysis_failed),
        ("stage3", "no_candidate", funnel.deep_analysis_no_candidate),
        ("stage4", "passed", funnel.risk_passed),
        ("stage4", "rejected", funnel.risk_rejected),
        ("publish", "published", funnel.published),
        ("publish", "outranked", funnel.final_outranked),
        ("publish", "capacity", funnel.capacity_rejected),
        ("any", "provider_failed", funnel.provider_failed),
        ("any", "data_stale", funnel.data_stale),
        ("any", "ai_budget_exhausted", funnel.ai_budget_exhausted),
    ):
        if count:
            METRICS.counter(
                "traido_scanner_candidates_total",
                count,
                labels={"stage": stage, "result": outcome},
                help_text="Instruments by the stage that decided them and the decision.",
            )

    for resource, stats in result.provider_stats.items():
        METRICS.counter(
            "traido_market_data_batch_requests",
            stats.get("calls", 0),
            labels={"resource": resource},
        )
        if stats.get("failures"):
            METRICS.counter(
                "traido_provider_errors_total",
                stats["failures"],
                labels={"resource": resource},
                help_text="Vendor reads that failed. A failure is never a pass.",
            )

    METRICS.gauge("traido_market_data_symbols_processed", funnel.market_filter_evaluated)
    METRICS.counter("traido_llm_calls_per_scan", float(result.ai_budget.get("calls_used", 0)))
    METRICS.gauge("traido_llm_cost_per_scan_usd", float(result.ai_budget.get("cost_used", 0.0)))
    if not funnel.reconciles():
        # Loud on purpose: an unbalanced funnel means a candidate was lost, and
        # the whole point of the ledger is that this cannot pass unnoticed.
        METRICS.counter("traido_scan_funnel_unbalanced_total")


def _cycle_detail() -> str:
    f = STATUS.funnel
    if f.paused_on_full_queue:
        return f"Cycle {STATUS.cycle} paused \u00b7 queue full"
    return (
        f"Cycle {STATUS.cycle} done \u00b7 {f.universe_total} scanned \u00b7 "
        f"{f.published} proposals"
    )


def _funnel_summary(funnel: ScanFunnel, cycle: int) -> str:
    """One line the operator can read the whole funnel from.

    Includes `reconciles`, because a funnel that does not balance has lost a
    candidate somewhere and that is worth seeing on the board rather than in a
    test.

    Provider failures are named whenever there are any. Without them the line
    reads the same whether Stage 3 found no setups or never got to look: a live
    cycle reported `deep 20 · risk-passed 0` while twenty symbols were being
    rate-limited out of the deep stage, and the summary the operator was given
    described a quiet market.
    """
    outcome = "paused on a full queue" if funnel.paused_on_full_queue else "finished"
    trouble = ""
    if funnel.provider_failed or funnel.data_stale:
        trouble = (
            f" \u00b7 provider-failed {funnel.provider_failed} \u00b7 stale {funnel.data_stale}"
        )
    return (
        f"Cycle {cycle} {outcome} \u00b7 universe {funnel.universe_total} \u00b7 "
        f"eligible {funnel.structurally_eligible} \u00b7 "
        f"market-passed {funnel.market_filter_passed} \u00b7 "
        f"shortlisted {funnel.quant_shortlisted} \u00b7 "
        f"deep {funnel.deep_analysis_started} \u00b7 "
        f"risk-passed {funnel.risk_passed} \u00b7 "
        f"published {funnel.published} \u00b7 "
        f"outranked {funnel.final_outranked}"
        f"{trouble} \u00b7 "
        f"reconciles {funnel.reconciles()}"
    )


async def wait_before_next_cycle(delay: float) -> None:
    """Wait out `delay`, unless someone asks for a cycle sooner."""
    deadline = time.monotonic() + delay
    while True:
        if _wake_token != _wake_seen:
            return
        left = deadline - time.monotonic()
        if left <= 0:
            return
        await asyncio.sleep(min(WAKE_POLL_SECONDS, left))


async def scanner_loop() -> None:
    """Run cycles on a cadence, never overlapping, and say when one overran.

    Cycle *n* is due at a fixed offset from the first, so a slow cycle does not
    push every later one back by however long it took. The old loop slept for
    the full interval *after* finishing, which turned a four-minute cycle and a
    five-minute interval into a nine-minute period that nothing reported.
    """
    global _wake_seen, _schedule
    while True:
        cfg = load_watchlist()
        interval = max(30.0, float(cfg.get("scan_interval_seconds") or 90))
        if _schedule is None:
            _schedule = ScanSchedule(interval_sec=interval)
        else:
            _schedule.retarget(interval)

        # Taken before the cycle, so a wake raised while it runs still counts
        # as pending afterwards instead of being swallowed.
        _wake_seen = _wake_token
        late = _schedule.begin()
        if late > 1.0:
            BOARD.log(
                "scanner",
                f"Cycle started {late:.0f}s after its slot",
                level="warn",
            )
        status = await run_scan_cycle()
        overrun = _schedule.complete()
        if overrun > 0:
            from core.metrics import METRICS

            METRICS.counter(
                "traido_scan_overrun_total",
                help_text="Cycles that ran past their own slot. Cadence, not latency.",
            )
            BOARD.log(
                "scanner",
                f"SCAN_OVERRUN \u00b7 cycle ran {overrun:.0f}s past its next slot",
                level="warn",
            )
        STATUS.schedule = _schedule.as_dict()

        # Full queue → retry soon (no work done). Empty confirm queue → hunt.
        # Only an open BUY earns the configured cadence.
        delay = choose_scan_delay(
            paused_on_full_queue=status.funnel.paused_on_full_queue,
            open_buys=open_buy_count(),
            interval=interval,
            seconds_until_due=_schedule.seconds_until_due(),
        )
        await wait_before_next_cycle(delay)


def start_scanner() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(scanner_loop(), name="traido-scanner")


def stop_scanner() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None
