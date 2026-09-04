"""One scan cycle, as a funnel rather than a loop.

The old cycle walked a capped list and gave every name the full treatment:
400 days of hourly bars, a week of news, technical, quant and strategy passes.
Measured, that was 3.95 seconds and 12.6 HTTP requests per symbol, which put the
ceiling at about seventy names per five-minute cadence — and the configured cap
of sixty sat just under it. There was no number to raise.

So the work is staged by cost instead. Each stage is cheaper per name than the
one after it and admits fewer names to it:

    Stage 0  structural eligibility   reference data, thousands of names
    Stage 1  cheap market filter      one batched snapshot read
    Stage 2  quant pre-ranking        one batched daily-bar read
    Stage 3  deep analysis            the existing per-symbol pipeline
    Stage 4  risk                     the existing deterministic engine
             rank → capacity → publish

Nothing about how a trade is judged changes. Stage 3 *is* `run_symbol_pipeline`
and Stage 4 *is* the risk engine; every gate that protects capital still runs, in
the same order, on everything that reaches them. What changed is how few names
reach them, and that the funnel can now say where all the others went.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from agents.scanner.funnel import ScanFunnel
from agents.scanner.prefilter import (
    MarketFilterPolicy,
    apply_market_filter,
)
from agents.scanner.prerank import PrerankPolicy, QuantCandidate, prerank
from core.activity import BOARD
from core.config import Settings, get_settings
from core.enums import UniverseMode
from core.schemas import PipelineResult
from trading.opportunities import OPPORTUNITIES, withdraw_unactionable
from trading.pipeline import publish_opportunity, run_symbol_pipeline
from trading.scan_context import ScanContext, open_scan_context
from universe.models import Instrument, UniverseTier
from universe.service import UniverseService

PRERANK_LOOKBACK_DAYS = 200
"""Daily history fetched for Stage 2.

Enough for a 50-day mean plus the 60-session range the proximity term uses, and
no more: this is a batched read over the Stage 1 survivors, so every extra day
is multiplied by a hundred and fifty names.
"""

_TIER_FOR_MODE = {
    UniverseMode.CORE: UniverseTier.CORE,
    UniverseMode.EXTENDED: UniverseTier.EXTENDED,
    UniverseMode.BROAD: UniverseTier.BROAD,
}


@dataclass
class StageTimings:
    """Seconds by stage. The first question about a slow cycle is always where."""

    universe: float = 0.0
    market_filter: float = 0.0
    prerank: float = 0.0
    deep_analysis: float = 0.0
    publish: float = 0.0
    total: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {k: round(v, 3) for k, v in vars(self).items()}


@dataclass
class CycleResult:
    """Everything one cycle produced, for the status object and the tests."""

    funnel: ScanFunnel = field(default_factory=ScanFunnel)
    timings: StageTimings = field(default_factory=StageTimings)
    published: list[str] = field(default_factory=list)
    shortlist: list[str] = field(default_factory=list)
    universe_symbols: list[str] = field(default_factory=list)
    provider_stats: dict[str, dict[str, float]] = field(default_factory=dict)
    ai_budget: dict[str, float | int] = field(default_factory=dict)
    error: str | None = None


def rank_key(result: PipelineResult) -> tuple[float, float, str]:
    """Strongest first, and the same order every run.

    Confidence, then geometry, then the symbol. The last term is what makes the
    ordering total: without it two equally confident candidates with equal
    reward-to-risk were left in whatever order they finished in, which under
    concurrency is decided by which provider response came back first. A desk
    whose proposals depend on network timing is not reproducible, and cannot be
    compared with itself from one day to the next.
    """
    candidate = result.candidate
    assert candidate is not None, "only risk-passed results are ranked"
    return (-candidate.confidence, -candidate.risk_reward, candidate.symbol.upper())


def _held_symbols() -> set[str]:
    from trading.ledger import LEDGER

    return {row.symbol.upper() for row in LEDGER.get_open()}


def _carded_symbols() -> set[str]:
    return {opp.candidate.symbol.upper() for opp in OPPORTUNITIES.list_open()}


async def run_cycle(
    *,
    settings: Settings | None = None,
    universe_service: UniverseService,
    timeframes: tuple,
    max_open: int,
    scheduled_at: datetime | None = None,
    context: ScanContext | None = None,
    on_progress: Callable[[ScanFunnel], None] | None = None,
) -> CycleResult:
    """Walk the funnel once.

    `context` is injectable so a benchmark or a test can supply deterministic
    vendors; production passes nothing and gets one built for the cycle.

    `on_progress` receives the live funnel object once, at the start. The desk
    then reads the same object while stages fill it — otherwise the card holds
    the previous cycle (or zeros) for the whole walk and looks stuck.
    """
    resolved = settings or get_settings()
    result = CycleResult()
    funnel = result.funnel
    if on_progress is not None:
        on_progress(funnel)
    started = time.monotonic()

    owns_context = context is None
    ctx = context or open_scan_context(resolved, scheduled_at=scheduled_at)
    try:
        await _run_stages(
            ctx,
            resolved,
            result,
            universe_service=universe_service,
            timeframes=timeframes,
            max_open=max_open,
        )
    finally:
        if owns_context:
            await ctx.aclose()
        result.timings.total = time.monotonic() - started
        result.provider_stats = ctx.concurrency.as_dict()
        result.ai_budget = ctx.ai_budget.as_dict()
        funnel.ai_budget_exhausted = len(ctx.ai_budget.exhausted_candidates)
    return result


async def _run_stages(
    ctx: ScanContext,
    settings: Settings,
    result: CycleResult,
    *,
    universe_service: UniverseService,
    timeframes: tuple,
    max_open: int,
) -> None:
    funnel = result.funnel

    # ── Housekeeping before the slots are counted ───────────────────────────
    # A slot held by a card that can no longer be bought must not be what stops
    # this cycle from looking.
    try:
        withdraw_unactionable(OPPORTUNITIES)
    except Exception as exc:  # noqa: BLE001
        BOARD.log("scanner", f"Could not sweep stale proposals: {exc!r}", level="warn")

    # ── Stage 0: what may we look at at all ────────────────────────────────
    t0 = time.monotonic()
    tier = _TIER_FOR_MODE.get(settings.universe_mode, UniverseTier.CORE)
    snapshot = await ctx.concurrency.run(
        "reference",
        lambda: universe_service.get_universe(tier=tier, max_size=settings.universe_max_size),
    )
    result.timings.universe = time.monotonic() - t0

    funnel.universe_total = snapshot.total
    funnel.structurally_eligible = len(snapshot.eligible)
    funnel.stage0_rejected = snapshot.rejected_count
    funnel.eligible_capped = int(getattr(snapshot, "capped_out", 0) or 0)
    funnel.stage0_reasons = dict(snapshot.rejection_reasons)
    result.universe_symbols = snapshot.symbols

    # The queue cap is read *after* the sweep, and the pass still records what
    # the universe was — a paused cycle that reports an empty universe cannot be
    # told apart from a broken provider.
    if len(OPPORTUNITIES.list_open()) >= max_open:
        funnel.paused_on_full_queue = True
        # Everything eligible is terminal for this cycle, under the reason that
        # is actually true: there was no slot to award it.
        funnel.capacity_rejected = funnel.structurally_eligible
        BOARD.log(
            "scanner",
            f"Paused before scanning — {max_open} proposals already awaiting a decision",
            level="warn",
        )
        return

    # Symbols the book already holds are terminal here, not analysed. One
    # position per symbol is enforced at the click regardless; asking now saves
    # the most expensive stage from producing a card that cannot be acted on.
    held = _held_symbols()
    carded = _carded_symbols()
    candidates: list[Instrument] = []
    for instrument in snapshot.eligible:
        if instrument.key in held:
            funnel.position_open += 1
        elif instrument.key in carded:
            funnel.duplicate_symbol_rejected += 1
        else:
            candidates.append(instrument)

    # ── Stage 1: one batched snapshot read over everything left ────────────
    t0 = time.monotonic()
    funnel.market_filter_evaluated = len(candidates)
    try:
        snapshots = await ctx.snapshots([i.key for i in candidates])
    except Exception as exc:  # noqa: BLE001
        # A failed batch is a failed batch, not an empty market. Everything it
        # covered is recorded as a provider failure so the funnel still balances
        # and the desk does not read "nothing was liquid today".
        funnel.provider_failed = len(candidates)
        result.error = f"snapshot_batch_failed: {exc!r}"
        BOARD.log("scanner", f"Snapshot batch failed: {exc!r}", level="error")
        result.timings.market_filter = time.monotonic() - t0
        return

    stage1 = apply_market_filter(
        candidates,
        snapshots,
        policy=MarketFilterPolicy(),
        limit=settings.market_prefilter_limit,
    )
    result.timings.market_filter = time.monotonic() - t0
    funnel.market_filter_passed = len(stage1.passed)
    funnel.market_filter_rejected = len(stage1.rejected)
    funnel.market_filter_reasons = stage1.reason_counts
    funnel.data_stale = stage1.reason_counts.get("STALE_DATA", 0)

    if not stage1.passed:
        return

    # ── Stage 2: one batched daily-bar read, then deterministic scoring ────
    t0 = time.monotonic()
    survivors = [c.instrument for c in stage1.passed]
    end = datetime.now(UTC)
    start = end - timedelta(days=PRERANK_LOOKBACK_DAYS)
    funnel.quant_evaluated = len(survivors)
    try:
        bars = await ctx.daily_bars([i.key for i in survivors], start, end)
    except Exception as exc:  # noqa: BLE001
        funnel.provider_failed += len(survivors)
        result.error = f"bars_batch_failed: {exc!r}"
        BOARD.log("scanner", f"Daily bar batch failed: {exc!r}", level="error")
        result.timings.prerank = time.monotonic() - t0
        return

    stage2 = prerank(
        survivors,
        bars,
        policy=PrerankPolicy(),
        top_k=settings.quant_top_k,
        now=end,
    )
    result.timings.prerank = time.monotonic() - t0
    funnel.quant_shortlisted = len(stage2.shortlist)
    funnel.quant_rejected = len(stage2.rejected)
    funnel.quant_outranked = len(stage2.outranked)
    funnel.quant_reasons = stage2.reason_counts
    result.shortlist = [c.symbol for c in stage2.shortlist]

    if not stage2.shortlist:
        return

    # ── Stage 3: deep analysis, on finalists only ──────────────────────────
    finalists = _apply_ai_budget(ctx, stage2.shortlist, settings, funnel)
    if not finalists:
        return

    t0 = time.monotonic()
    funnel.deep_analysis_started = len(finalists)

    async def _analyse(candidate: QuantCandidate) -> PipelineResult:
        return await run_symbol_pipeline(
            candidate.symbol,
            timeframes=timeframes,
            settings=settings,
            publish=False,
            context=ctx,
        )

    outcomes = await ctx.concurrency.map("deep", finalists, _analyse)
    result.timings.deep_analysis = time.monotonic() - t0

    passed: list[PipelineResult] = []
    for candidate, outcome in zip(finalists, outcomes, strict=True):
        if isinstance(outcome, BaseException):
            # One symbol's provider error must not kill the scan, and must not
            # vanish either.
            funnel.deep_analysis_failed += 1
            BOARD.log(
                "scanner",
                f"{candidate.symbol} failed: {outcome!r}",
                symbol=candidate.symbol,
                level="error",
            )
            continue
        _record_deep_outcome(outcome, funnel, passed)

    if not passed:
        return

    # ── Rank, then capacity, then publish ──────────────────────────────────
    t0 = time.monotonic()
    await _rank_and_publish(passed, funnel, result, settings=settings, max_open=max_open)
    result.timings.publish = time.monotonic() - t0


def _apply_ai_budget(
    ctx: ScanContext,
    shortlist: list[QuantCandidate],
    settings: Settings,
    funnel: ScanFunnel,
) -> list[QuantCandidate]:
    """Cut the shortlist to what Stage 3 may afford, in ranked order.

    The budget is spent from the top of the deterministic pre-ranking, never at
    random and never in completion order, so an exhausted budget shortens the
    list without changing which names were preferred. Everything refused is
    counted, so the funnel still balances.
    """
    deep_cap = max(0, settings.deep_analysis_top_k)
    finalists: list[QuantCandidate] = []
    for index, candidate in enumerate(shortlist):
        if index >= deep_cap:
            funnel.deep_analysis_outranked += 1
            continue
        if not ctx.ai_budget.take_candidate(candidate.symbol):
            continue
        finalists.append(candidate)
    return finalists


def _record_deep_outcome(
    outcome: PipelineResult,
    funnel: ScanFunnel,
    passed: list[PipelineResult],
) -> None:
    status = outcome.status
    if status == "position_open":
        # Raced with a fill that landed mid-cycle.
        funnel.position_open += 1
        return
    if status == "awaiting_confirmation":
        funnel.duplicate_symbol_rejected += 1
        return
    if outcome.candidate is None:
        if status == "failed":
            funnel.deep_analysis_failed += 1
        else:
            funnel.deep_analysis_no_candidate += 1
        return

    # Had a candidate through deep analysis. Terminal disposition is exactly one
    # of wait_for_entry / risk_rejected / risk_passed (later
    # published|outranked|capacity) / no_candidate (NO_TRADE / etc.). Do not
    # also bump `passed` on the WAIT path — that made started=20 look like
    # 35 outcomes. WAIT is not `no_candidate`: that is how a desk full of
    # watches reported risk 0 · published 0.
    if status == "wait_for_entry":
        funnel.wait_for_entry += 1
        return
    if status == "data_blocked":
        funnel.data_blocked += 1
        return
    if status == "operational_blocked":
        funnel.operational_blocked += 1
        return
    if status == "no_trade":
        funnel.deep_analysis_no_candidate += 1
        return
    if status == "risk_rejected":
        funnel.deep_analysis_passed += 1
        funnel.risk_rejected += 1
        for reason in getattr(outcome.risk, "reasons", []) or []:
            funnel.rejection_reasons[reason] = funnel.rejection_reasons.get(reason, 0) + 1
        return
    if status == "risk_passed":
        funnel.deep_analysis_passed += 1
        funnel.risk_passed += 1
        passed.append(outcome)
        return
    funnel.deep_analysis_no_candidate += 1


async def _rank_and_publish(
    passed: list[PipelineResult],
    funnel: ScanFunnel,
    result: CycleResult,
    *,
    settings: Settings,
    max_open: int,
) -> None:
    """Rank everything, then spend capacity, then re-check, then publish.

    The order is the whole point. Capacity is read *after* ranking, so a slot is
    never reserved by a name that a later, stronger one would have taken — that
    is what publishing as you go did, and it meant the desk saw the earliest
    qualifying names rather than the best ones.

    The re-check before each publication is not a formality. Ranking a hundred
    and fifty names takes time, and in that time a fill can land, another cycle
    can card the symbol, or the queue can fill. Each of those makes publishing
    wrong for a different reason, so each is asked again immediately before the
    card is written.
    """
    ranked = sorted(passed, key=rank_key)
    free_at_start = max(0, max_open - len(OPPORTUNITIES.list_open()))

    for entry in ranked:
        assert entry.candidate is not None
        symbol = entry.candidate.symbol.upper()

        free = max(0, max_open - len(OPPORTUNITIES.list_open()))
        if free <= 0:
            # Two different facts, deliberately not merged. "The desk was
            # already full" is an operator problem — cards are waiting for a
            # decision. "This cycle filled it" means the funnel worked and found
            # more than it could offer. A desk that reports only one of them
            # cannot tell a backlog from a good day.
            if free_at_start <= 0:
                funnel.capacity_rejected += 1
            else:
                funnel.final_outranked += 1
            continue
        if symbol in _held_symbols():
            funnel.position_open += 1
            continue
        if symbol in _carded_symbols():
            funnel.duplicate_symbol_rejected += 1
            continue

        try:
            await publish_opportunity(
                entry,
                entry.risk,
                settings=settings,
                admission=entry.trade_admission,
            )
        except Exception as exc:  # noqa: BLE001
            funnel.deep_analysis_failed += 1
            BOARD.log("scanner", f"{symbol} publish failed: {exc!r}", symbol=symbol, level="error")
            continue
        funnel.published += 1
        result.published.append(symbol)
