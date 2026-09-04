"""Funnel accounting, concurrency budgets, the AI budget, cadence, freshness.

The machinery around the stages rather than the stages themselves. Each has the
same shape of failure: it works quietly until the universe is large enough that
the thing it was protecting against actually happens.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from agents.scanner.funnel import ScanFunnel
from agents.scanner.schedule import ScanSchedule
from core.concurrency import (
    DEFAULT_BUDGETS,
    AIBudget,
    ConcurrencyManager,
    RateLimiter,
    ResourceBudget,
)
from core.enums import Timeframe
from core.freshness import FreshnessCache
from market_data.providers.alpaca import _bar_from_alpaca, _snapshot_from_alpaca

# ── Funnel accounting ───────────────────────────────────────────────────────


def _balanced_funnel() -> ScanFunnel:
    """A cycle of 100 names where every one of them ends somewhere terminal."""
    funnel = ScanFunnel()
    funnel.universe_total = 100
    funnel.structurally_eligible = 80
    funnel.stage0_rejected = 20
    funnel.market_filter_evaluated = 80
    funnel.market_filter_passed = 30
    funnel.market_filter_rejected = 50
    funnel.quant_evaluated = 30
    funnel.quant_shortlisted = 10
    funnel.quant_rejected = 5
    funnel.quant_outranked = 15
    funnel.deep_analysis_started = 10
    funnel.deep_analysis_passed = 7
    funnel.deep_analysis_no_candidate = 3
    funnel.risk_passed = 4
    funnel.risk_rejected = 3
    funnel.published = 2
    funnel.final_outranked = 2
    return funnel


def test_a_balanced_cycle_reconciles() -> None:
    funnel = _balanced_funnel()

    assert funnel.reconciles()
    assert funnel.unaccounted() == 0


def test_a_candidate_that_vanishes_breaks_the_invariant() -> None:
    """The point of the ledger: silence becomes detectable.

    A name that is neither published nor rejected for a stated reason is a name
    the operator cannot ask about, and the honest answer to "why did it not look
    at the one I expected" would be "we do not know".
    """
    funnel = _balanced_funnel()
    funnel.risk_rejected -= 1

    assert not funnel.reconciles()
    assert funnel.unaccounted() == 1


def test_counting_a_name_twice_breaks_it_in_the_other_direction() -> None:
    funnel = _balanced_funnel()
    funnel.published += 1

    assert not funnel.reconciles()
    assert funnel.unaccounted() == -1


def test_a_name_never_looked_at_is_not_filed_as_having_no_setup() -> None:
    """`position_open` and `no_candidate` are both terminal and not the same.

    One says the book already holds it, the other says we analysed it and found
    nothing. Merging them turns "we could not look" into "there was nothing
    there", which is the reading that hides a bug.
    """
    funnel = ScanFunnel()
    funnel.universe_total = 3
    funnel.structurally_eligible = 3
    funnel.deep_analysis_no_candidate = 1
    funnel.position_open = 1
    funnel.provider_failed = 1

    assert funnel.reconciles()


def test_ai_budget_exhaustion_is_accounted_not_hidden() -> None:
    """Names dropped for cost are still names dropped."""
    funnel = ScanFunnel()
    funnel.universe_total = 10
    funnel.structurally_eligible = 10
    funnel.market_filter_passed = 10
    funnel.quant_shortlisted = 10
    funnel.deep_analysis_started = 6
    funnel.ai_budget_exhausted = 4
    funnel.risk_passed = 1
    funnel.risk_rejected = 2
    funnel.deep_analysis_no_candidate = 3
    funnel.published = 1

    assert funnel.reconciles()


def test_top_rejections_are_ordered_by_count_then_name() -> None:
    """Ties must not be ordered by whichever reason the provider produced first."""
    funnel = ScanFunnel()
    funnel.rejection_reasons = {"SPREAD_TOO_WIDE": 3, "EARNINGS": 9, "ADV": 3}

    assert funnel.top_rejections(n=3) == [("EARNINGS", 9), ("ADV", 3), ("SPREAD_TOO_WIDE", 3)]


def test_a_reset_clears_the_previous_cycle() -> None:
    """Counters that survive a reset turn the funnel into a running total."""
    funnel = _balanced_funnel()
    funnel.reset()

    assert funnel.universe_total == 0
    assert funnel.published == 0
    assert funnel.rejection_reasons == {}


def test_a_paused_cycle_is_distinguishable_from_an_empty_one() -> None:
    funnel = ScanFunnel()
    funnel.paused_on_full_queue = True

    assert funnel.as_dict()["paused_on_full_queue"] is True


def test_eligible_cap_is_a_terminal_bucket() -> None:
    """Names cut by universe_max_size after Stage 0 must not look 'lost'."""
    funnel = ScanFunnel()
    funnel.universe_total = 100
    funnel.structurally_eligible = 20
    funnel.stage0_rejected = 10
    funnel.eligible_capped = 70
    funnel.market_filter_rejected = 15
    funnel.quant_outranked = 3
    funnel.deep_analysis_no_candidate = 2

    assert funnel.reconciles()
    assert funnel.unaccounted() == 0
    assert funnel.as_dict()["eligible_capped"] == 70


def test_wait_outcome_is_not_also_deep_passed() -> None:
    from uuid import uuid4

    from agents.scanner.cycle import _record_deep_outcome
    from core.schemas import PipelineResult, TradeCandidate
    from core.enums import TradeAction
    from decimal import Decimal

    funnel = ScanFunnel()
    funnel.universe_total = 1
    cand = TradeCandidate(
        symbol="WAIT",
        action=TradeAction.BUY,
        confidence=0.5,
        entry=Decimal(10),
        stop=Decimal(9),
        target=Decimal(12),
        risk_reward=2.0,
        reasons=["x"],
        strategy_version="t@1",
    )
    _record_deep_outcome(
        PipelineResult(
            pipeline_run_id=uuid4(),
            symbol="WAIT",
            status="wait_for_entry",
            candidate=cand,
        ),
        funnel,
        [],
    )
    assert funnel.deep_analysis_passed == 0
    assert funnel.deep_analysis_no_candidate == 1
    assert funnel.reconciles()


def test_the_dict_carries_the_verdict_not_just_the_counters() -> None:
    data = _balanced_funnel().as_dict()

    assert data["reconciles"] is True
    assert data["terminal_total"] == 100


# ── Concurrency and rate limits ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrency_is_bounded_per_resource() -> None:
    """`asyncio.gather` over a thousand symbols is the thing being prevented."""
    manager = ConcurrencyManager({"md": ResourceBudget("md", max_concurrency=3)})
    live = 0
    peak = 0

    async def work() -> None:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1

    await asyncio.gather(*(manager.run("md", work) for _ in range(20)))

    assert peak == 3


@pytest.mark.asyncio
async def test_separate_resources_do_not_share_a_budget() -> None:
    """Market data queued behind the LLM's semaphore would serialise the cycle."""
    manager = ConcurrencyManager(
        {
            "md": ResourceBudget("md", max_concurrency=1),
            "llm": ResourceBudget("llm", max_concurrency=1),
        }
    )

    async def hold() -> None:
        await asyncio.sleep(0.05)

    # Two resources, one slot each: serial would take 0.1s, parallel 0.05s.
    await asyncio.wait_for(
        asyncio.gather(manager.run("md", hold), manager.run("llm", hold)), timeout=0.09
    )


@pytest.mark.asyncio
async def test_an_unnamed_resource_still_gets_a_budget() -> None:
    """A typo must not silently mean unbounded concurrency."""
    manager = ConcurrencyManager({})
    live = 0
    peak = 0

    async def work() -> None:
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.005)
        live -= 1

    await asyncio.gather(*(manager.run("typo", work) for _ in range(40)))

    assert peak <= ResourceBudget("typo").max_concurrency


@pytest.mark.asyncio
async def test_the_rate_limiter_paces_a_burst() -> None:
    """Ten calls at 50/sec cannot all leave in the first millisecond."""
    limiter = RateLimiter(50.0, burst=2)
    started = time.monotonic()

    for _ in range(10):
        await limiter.acquire()

    assert time.monotonic() - started >= 0.1


@pytest.mark.asyncio
async def test_a_429_penalty_blocks_acquires() -> None:
    """After Alpaca says slow down, the bucket must not refill into the ban."""
    limiter = RateLimiter(100.0, burst=5)
    await limiter.penalize(0.15)
    started = time.monotonic()
    await limiter.acquire()
    assert time.monotonic() - started >= 0.12


@pytest.mark.asyncio
async def test_a_pool_larger_than_the_rate_still_obeys_the_rate() -> None:
    """Eight fast responses simply come back sooner and start eight more."""
    manager = ConcurrencyManager(
        {"md": ResourceBudget("md", max_concurrency=8, rate_per_sec=40.0, burst=1)}
    )

    async def work() -> None:
        return None

    started = time.monotonic()
    await asyncio.gather(*(manager.run("md", work) for _ in range(8)))

    assert time.monotonic() - started >= 0.15


@pytest.mark.asyncio
async def test_one_symbols_failure_is_returned_rather_than_raised() -> None:
    """A provider error on one name must not kill the scan — or vanish."""
    manager = ConcurrencyManager({"md": ResourceBudget("md", max_concurrency=2)})

    async def work(symbol: str) -> str:
        if symbol == "BAD":
            raise RuntimeError("vendor 503")
        return symbol

    results = await manager.map("md", ["AAA", "BAD", "CCC"], work)

    assert results[0] == "AAA"
    assert isinstance(results[1], RuntimeError)
    assert results[2] == "CCC"


@pytest.mark.asyncio
async def test_results_keep_input_order_not_completion_order() -> None:
    """Downstream ranking is positional; reordering here would leak timing in."""
    manager = ConcurrencyManager({"md": ResourceBudget("md", max_concurrency=4)})

    async def work(delay: float) -> float:
        await asyncio.sleep(delay)
        return delay

    assert await manager.map("md", [0.03, 0.01, 0.02], work) == [0.03, 0.01, 0.02]


@pytest.mark.asyncio
async def test_a_failure_is_counted_against_the_resource() -> None:
    manager = ConcurrencyManager({"md": ResourceBudget("md", max_concurrency=2)})

    async def boom() -> None:
        raise RuntimeError("vendor 503")

    await manager.map("md", [1, 2], lambda _: boom())

    assert manager.stats["md"].calls == 2
    assert manager.stats["md"].failures == 2


@pytest.mark.asyncio
async def test_a_timeout_is_a_failure_not_a_pass() -> None:
    """A provider that never answers must not resolve to an empty success."""
    manager = ConcurrencyManager({"md": ResourceBudget("md", max_concurrency=1, timeout_sec=0.02)})

    async def never() -> None:
        await asyncio.sleep(5)

    results = await manager.map("md", [1], lambda _: never())

    assert isinstance(results[0], BaseException)
    assert manager.stats["md"].timeouts == 1


def test_the_shipped_budgets_are_all_bounded() -> None:
    assert DEFAULT_BUDGETS
    for budget in DEFAULT_BUDGETS.values():
        assert 0 < budget.max_concurrency <= 32
        assert budget.timeout_sec > 0


def test_the_llm_budget_is_among_the_tightest_shipped() -> None:
    """LLM mistakes cost money; deep stays at one to protect the data quota."""
    llm = DEFAULT_BUDGETS["llm"].max_concurrency
    deep = DEFAULT_BUDGETS["deep"].max_concurrency
    assert deep == 1
    assert llm <= 2
    assert llm <= min(
        b.max_concurrency for b in DEFAULT_BUDGETS.values() if b.name not in {"broker", "deep"}
    )


# ── AI budget ───────────────────────────────────────────────────────────────


def test_the_ai_budget_takes_candidates_in_the_order_it_is_offered_them() -> None:
    """Exhaustion consumes the pre-ranking order, never an arrival order."""
    budget = AIBudget(max_candidates=3, max_calls=100)
    ranked = ["AAA", "BBB", "CCC", "DDD", "EEE"]

    taken = [s for s in ranked if budget.take_candidate(s)]

    assert taken == ["AAA", "BBB", "CCC"]
    assert budget.exhausted_candidates == ["DDD", "EEE"]


def test_the_same_ranking_gives_the_same_cut_every_time() -> None:
    ranked = ["AAA", "BBB", "CCC"]

    def run() -> list[str]:
        budget = AIBudget(max_candidates=2, max_calls=100)
        return [s for s in ranked if budget.take_candidate(s)]

    assert run() == run()


def test_a_call_ceiling_binds_as_well_as_a_candidate_ceiling() -> None:
    budget = AIBudget(max_candidates=10, max_calls=2)
    budget.record_call()
    budget.record_call()

    assert budget.take_candidate("AAA") is False


def test_a_cost_ceiling_binds_too() -> None:
    budget = AIBudget(max_candidates=10, max_calls=10, max_cost_usd=1.0)
    budget.record_call(cost_usd=1.5)

    assert budget.take_candidate("AAA") is False


def test_a_token_ceiling_binds_too() -> None:
    budget = AIBudget(max_candidates=10, max_calls=10, max_tokens=100)
    budget.record_call(tokens=250)

    assert budget.take_candidate("AAA") is False


def test_a_refused_candidate_is_named_so_the_funnel_can_count_it() -> None:
    budget = AIBudget(max_candidates=0, max_calls=10)
    budget.take_candidate("AAA")

    assert budget.exhausted_candidates == ["AAA"]
    assert budget.as_dict()["exhausted"] == 1


# ── Scheduling ──────────────────────────────────────────────────────────────


def test_a_fast_cycle_waits_for_its_slot_rather_than_sleeping_a_full_interval() -> None:
    """`scan; sleep(300)` after a 240s cycle is a nine-minute cadence, silently."""
    schedule = ScanSchedule(interval_sec=300.0, _origin=1000.0)

    schedule.begin(now=1000.0)
    overrun = schedule.complete(now=1048.0)

    assert overrun == 0.0
    assert schedule.seconds_until_due(now=1048.0) == pytest.approx(252.0)


def test_an_overrunning_cycle_is_reported_rather_than_absorbed() -> None:
    schedule = ScanSchedule(interval_sec=300.0, _origin=1000.0)

    schedule.begin(now=1000.0)
    overrun = schedule.complete(now=1420.0)

    assert overrun == pytest.approx(120.0)
    assert schedule.overruns == 1
    assert schedule.last_overrun_sec == pytest.approx(120.0)


def test_the_next_slot_is_never_in_the_past() -> None:
    """Otherwise a recovered scanner runs cycles back to back with no gap —
    bursting the provider budget exactly when it is already behind."""
    schedule = ScanSchedule(interval_sec=60.0, _origin=1000.0)

    schedule.begin(now=1000.0)
    schedule.complete(now=1500.0)

    assert schedule.next_due() > 1500.0


def test_missed_slots_are_skipped_not_queued() -> None:
    schedule = ScanSchedule(interval_sec=60.0, _origin=1000.0)

    schedule.begin(now=1000.0)
    schedule.complete(now=1500.0)

    # Eight slots passed while the cycle ran; the next is the ninth, not the second.
    assert schedule.seconds_until_due(now=1500.0) <= 60.0


def test_cadence_phase_survives_an_overrun() -> None:
    """Cycles stay aligned to the same points in the session, however many missed."""
    schedule = ScanSchedule(interval_sec=60.0, _origin=1000.0)
    schedule.begin(now=1000.0)
    schedule.complete(now=1500.0)

    assert (schedule.next_due() - 1000.0) % 60.0 == pytest.approx(0.0)


def test_the_schedule_measures_the_duration_it_saw() -> None:
    schedule = ScanSchedule(interval_sec=300.0, _origin=1000.0)

    schedule.begin(now=1005.0)
    schedule.complete(now=1053.0)

    assert schedule.last_duration_sec == pytest.approx(48.0)


def test_a_late_start_is_reported_to_the_caller() -> None:
    schedule = ScanSchedule(interval_sec=300.0, _origin=1000.0)

    lateness = schedule.begin(now=1030.0)

    assert lateness == pytest.approx(30.0)


def test_retargeting_does_not_put_the_next_slot_in_the_past() -> None:
    schedule = ScanSchedule(interval_sec=300.0, _origin=1000.0)

    schedule.retarget(30.0, now=2000.0)

    assert schedule.next_due() == pytest.approx(2030.0)


# ── Freshness ───────────────────────────────────────────────────────────────


def test_a_cached_value_is_served_within_its_ttl() -> None:
    cache: FreshnessCache[str] = FreshnessCache()
    cache.put("k", "v", ttl_sec=60.0, input_version="v1")

    hit = cache.get("k", input_version="v1")

    assert hit is not None
    assert hit.value == "v"


def test_an_expired_value_is_not_served() -> None:
    cache: FreshnessCache[str] = FreshnessCache()
    cache.put("k", "v", ttl_sec=0.0, input_version="v1")

    assert cache.get("k", input_version="v1") is None


def test_a_value_computed_under_different_inputs_is_wrong_not_merely_stale() -> None:
    """Same key, different policy. Serving it enforces yesterday's rules while
    the screen shows today's configuration."""
    cache: FreshnessCache[str] = FreshnessCache()
    cache.put("k", "v", ttl_sec=600.0, input_version="v1")

    assert cache.get("k", input_version="v2") is None


def test_an_entry_knows_when_it_was_computed_and_when_it_dies() -> None:
    cache: FreshnessCache[str] = FreshnessCache()
    cache.put("k", "v", ttl_sec=60.0, input_version="v1")
    entry = cache.peek("k")

    assert entry is not None
    assert entry.expires_at > entry.computed_at
    assert entry.age_sec() >= 0.0


def test_an_entry_can_carry_the_event_time_of_its_data() -> None:
    """A daily bar read at noon belongs to yesterday's close, and freshness
    questions about the data have to be asked of that, not of the read."""
    cache: FreshnessCache[str] = FreshnessCache()
    event = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)
    cache.put("k", "v", ttl_sec=60.0, input_version="v1", source_event_time=event)

    entry = cache.peek("k")

    assert entry is not None
    assert entry.source_event_time == event


def test_peek_shows_a_stale_entry_that_get_refuses() -> None:
    """Reporting how old the universe is must not be able to serve it."""
    cache: FreshnessCache[str] = FreshnessCache()
    cache.put("k", "v", ttl_sec=0.0, input_version="v1")

    assert cache.peek("k") is not None
    assert cache.get("k", input_version="v1") is None


def test_invalidation_drops_one_key_or_all_of_them() -> None:
    cache: FreshnessCache[str] = FreshnessCache()
    cache.put("a", "1", ttl_sec=60.0, input_version="v")
    cache.put("b", "2", ttl_sec=60.0, input_version="v")

    cache.invalidate("a")
    assert cache.peek("a") is None
    assert cache.peek("b") is not None

    cache.invalidate()
    assert cache.peek("b") is None


# ── Batch normalisation ─────────────────────────────────────────────────────


def test_an_alpaca_snapshot_is_normalised() -> None:
    snapshot = _snapshot_from_alpaca(
        "aapl",
        {
            "latestTrade": {"p": 232.5, "t": "2026-08-31T18:00:00+00:00"},
            "latestQuote": {"bp": 232.4, "ap": 232.6, "t": "2026-08-31T18:00:01+00:00"},
            "dailyBar": {"o": 230, "h": 233, "l": 229, "c": 232.5, "v": 40_000_000},
            "prevDailyBar": {"c": 229.8},
        },
    )

    assert snapshot.symbol == "AAPL"
    assert snapshot.price == Decimal("232.5")
    assert snapshot.day_volume == Decimal(40_000_000)
    assert snapshot.prev_close == Decimal("229.8")
    assert snapshot.trade_ts is not None


def test_a_snapshot_without_a_trade_falls_back_to_the_daily_close() -> None:
    """A halted name has no last trade and is still a real instrument."""
    snapshot = _snapshot_from_alpaca("AAPL", {"dailyBar": {"c": 100, "v": 1_000}})

    assert snapshot.price == Decimal(100)


def test_an_empty_payload_stays_absent_rather_than_becoming_zero() -> None:
    """`price=0` reads as free and `day_volume=0` as illiquid — the two most
    dangerous readings available. Stage 1 rejects `None`; it would rank a zero."""
    snapshot = _snapshot_from_alpaca("AAPL", {})

    assert snapshot.price is None
    assert snapshot.day_volume is None


def test_a_malformed_number_does_not_become_zero() -> None:
    snapshot = _snapshot_from_alpaca("AAPL", {"dailyBar": {"c": "not-a-number", "v": 10}})

    assert snapshot.price is None


def test_a_crossed_book_in_a_snapshot_reports_no_spread() -> None:
    snapshot = _snapshot_from_alpaca(
        "AAPL", {"latestQuote": {"bp": 101, "ap": 99}, "dailyBar": {"c": 100, "v": 10}}
    )

    assert snapshot.spread_bps is None


def test_an_alpaca_bar_is_normalised_with_its_symbol() -> None:
    bar = _bar_from_alpaca(
        "msft",
        Timeframe.D1,
        {"t": "2026-08-28T04:00:00+00:00", "o": 500, "h": 510, "l": 498, "c": 505, "v": 2e7},
    )

    assert bar is not None
    assert bar.symbol == "MSFT"
    assert bar.close == Decimal(505)


def test_one_malformed_row_does_not_cost_the_whole_batch() -> None:
    """Per symbol this only failed one request. In a batch of a thousand it
    would take every name's history out of the cycle at once."""
    assert _bar_from_alpaca("MSFT", Timeframe.D1, {"t": "2026-08-28T04:00:00+00:00"}) is None


def test_a_bar_without_a_timestamp_is_dropped() -> None:
    """An undated bar cannot be aged, and an unageable bar is a stale bar."""
    assert _bar_from_alpaca("MSFT", Timeframe.D1, {"o": 1, "h": 1, "l": 1, "c": 1, "v": 1}) is None
