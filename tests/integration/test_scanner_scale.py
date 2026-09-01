"""A thousand instruments through the funnel, against deterministic vendors.

These are the tests the refactor exists for. At sixty hand-picked names none of
them can fail; the failures they describe only appear once the universe is large
enough that the scanner has to *choose*, and the way it chooses stops being
obvious.

Three properties, each of which the old scanner violated:

1. Expensive analysis is applied to finalists, not to the universe. The old
   cycle ran the full pipeline on every symbol it reached, which is why it could
   only reach about sixty of them.
2. The best candidate wins from anywhere in the universe. The old cycle
   published as it went and capped its pass, so a strong name at the end was
   never reached, let alone ranked.
3. The result is a function of the data alone. With workers running
   concurrently, anything that depends on which response arrived first is a
   result that changes between runs for no reason a person can see.

No network: everything answers from `tests.scanner_fakes`, so CI does not depend
on a vendor being up or fast.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from agents.scanner import cycle as scan_cycle
from agents.scanner.cycle import run_cycle
from core.enums import RiskVerdict, TradeAction
from core.schemas import PipelineResult, RiskDecision, TradeCandidate
from risk.limits import default_risk_limits
from tests.scanner_fakes import (
    FakeMarketData,
    FakeUniverseProvider,
    fake_scan_context,
    flat_portfolio,
    make_symbol,
    scanner_settings,
)
from universe.service import UniverseService

UNIVERSE_SIZE = 1000


def _service(count: int) -> UniverseService:
    provider = FakeUniverseProvider(count)
    return UniverseService(provider, curated_provider=provider)


def _risk_pass() -> RiskDecision:
    return RiskDecision(
        verdict=RiskVerdict.PASS,
        sized_qty=Decimal(10),
        limits_applied=default_risk_limits(),
        portfolio=flat_portfolio(),
    )


def _passed(symbol: str, *, confidence: float, risk_reward: float = 2.0) -> PipelineResult:
    return PipelineResult(
        pipeline_run_id=uuid4(),
        symbol=symbol,
        status="risk_passed",
        candidate=TradeCandidate(
            symbol=symbol,
            action=TradeAction.BUY,
            confidence=confidence,
            entry=Decimal(100),
            stop=Decimal(95),
            target=Decimal(110),
            risk_reward=risk_reward,
            reasons=["test"],
            strategy_version="test@1",
        ),
        risk=_risk_pass(),
    )


class Desk:
    """A desk that accepts cards and can be emptied between runs.

    The queue has to be cleared explicitly, because several tests below run the
    cycle more than once and a desk left holding five cards pauses the next
    cycle before it looks at anything — which would make a determinism test pass
    by comparing four empty results.
    """

    def __init__(self) -> None:
        self.published: list[str] = []
        self.queue: list[object] = []

    def reset(self) -> None:
        self.published.clear()
        self.queue.clear()


@pytest.fixture
def desk(monkeypatch: pytest.MonkeyPatch) -> Desk:
    """Silence the board and give the cycle an empty, writable desk."""
    monkeypatch.setattr(scan_cycle.BOARD, "log", lambda *a, **k: None)
    monkeypatch.setattr(scan_cycle.BOARD, "set_agent", lambda *a, **k: None)
    monkeypatch.setattr(scan_cycle, "withdraw_unactionable", lambda *a, **k: 0)
    monkeypatch.setattr(scan_cycle, "_held_symbols", set)
    monkeypatch.setattr(scan_cycle, "_carded_symbols", set)

    board = Desk()
    monkeypatch.setattr(scan_cycle.OPPORTUNITIES, "list_open", lambda: list(board.queue))

    async def _publish(result: PipelineResult, _risk: object, **_kw: object) -> PipelineResult:
        board.published.append(result.symbol)
        board.queue.append(object())
        return result

    monkeypatch.setattr(scan_cycle, "publish_opportunity", _publish)
    return board


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    desk: Desk,
    *,
    count: int = UNIVERSE_SIZE,
    strongest: str | None = None,
    shuffle_seed: int | None = None,
    settings=None,
    analysed: list[str] | None = None,
    confidences: dict[str, float] | None = None,
    max_open: int = 5,
):
    desk.reset()
    resolved = settings or scanner_settings(
        TRAIDO_MARKET_PREFILTER_LIMIT=150,
        TRAIDO_QUANT_TOP_K=30,
        TRAIDO_DEEP_ANALYSIS_TOP_K=20,
        TRAIDO_MAX_LLM_CANDIDATES=20,
    )
    feed = FakeMarketData(strongest=strongest, shuffle_seed=shuffle_seed)
    ctx = fake_scan_context(resolved, market_data=feed)

    async def _pipeline(symbol: str, **_kw: object) -> PipelineResult:
        if analysed is not None:
            analysed.append(symbol)
        conf = (confidences or {}).get(symbol, 0.5)
        return _passed(symbol, confidence=conf)

    monkeypatch.setattr(scan_cycle, "run_symbol_pipeline", _pipeline)

    result = await run_cycle(
        settings=resolved,
        universe_service=_service(count),
        timeframes=(),
        max_open=max_open,
        context=ctx,
    )
    return result, feed


@pytest.mark.asyncio
async def test_a_thousand_instruments_run_end_to_end(
    monkeypatch: pytest.MonkeyPatch, desk: Desk
) -> None:
    """The whole funnel, at the target size, with every stage narrowing."""
    analysed: list[str] = []
    result, _feed = await _run(monkeypatch, desk, analysed=analysed)
    f = result.funnel

    assert f.universe_total == UNIVERSE_SIZE
    assert f.structurally_eligible == UNIVERSE_SIZE
    assert f.market_filter_evaluated == UNIVERSE_SIZE
    assert f.market_filter_passed == 150, "the prefilter limit must bind"
    assert f.quant_shortlisted == 30
    assert f.deep_analysis_started == 20
    assert len(analysed) == 20
    assert desk.published == result.published
    assert len(desk.published) == 5, "the desk's five slots, filled by the best of the shortlist"


@pytest.mark.asyncio
async def test_only_finalists_reach_the_expensive_stage(
    monkeypatch: pytest.MonkeyPatch, desk: Desk
) -> None:
    """A thousand names, twenty deep analyses.

    Red without the fix: the old cycle called `run_symbol_pipeline` for every
    symbol in its slice, so this count would equal the universe size — and at a
    measured 3.95 s per symbol, a thousand of them is 66 minutes.
    """
    analysed: list[str] = []
    result, feed = await _run(monkeypatch, desk, analysed=analysed)

    assert len(analysed) == 20
    assert len(analysed) < UNIVERSE_SIZE / 40
    assert set(analysed) <= set(result.shortlist)

    # It is the *deep cap* that cut the shortlist from 30 to 20, not the AI
    # budget. The two limits are both configured at 20 here, so a count alone
    # cannot tell them apart — and a broken deep cap would still show twenty
    # analyses because the budget caught the overflow. The funnel can tell:
    # names cut by the cap are `quant_outranked`, names refused for cost are
    # `ai_budget_exhausted`, and confusing the two hides which limit is binding.
    assert result.funnel.deep_analysis_outranked == 10
    assert result.funnel.ai_budget_exhausted == 0

    # And the cheap stages did their thousand names in a handful of requests.
    assert feed.snapshot_requests == 1
    assert feed.snapshot_symbols == UNIVERSE_SIZE
    assert feed.bar_requests == 1
    assert feed.bar_symbols == 150


@pytest.mark.asyncio
async def test_the_strongest_candidate_placed_last_still_ranks_first(
    monkeypatch: pytest.MonkeyPatch, desk: Desk
) -> None:
    """999 weak names and one strong one at the very end of the universe.

    This is the mandated regression, and it fails in two distinct ways on the
    old design: the per-cycle cap never reaches the last symbol, and publishing
    as the pass goes would have spent the slots long before arriving.
    """
    last = make_symbol(UNIVERSE_SIZE - 1)
    result, _feed = await _run(
        monkeypatch,
        desk,
        strongest=last,
        confidences={last: 0.99},
    )

    assert last in result.shortlist, "a strong name at the end never reached the shortlist"
    assert result.published[0] == last, f"published in the wrong order: {result.published}"


@pytest.mark.asyncio
async def test_publication_order_follows_conviction_not_the_quant_shortlist(
    monkeypatch: pytest.MonkeyPatch, desk: Desk
) -> None:
    """The final order is the risk-passed candidates' own, re-sorted.

    Stage 2's ranking decides who gets *analysed*; it must not decide who gets
    *published*. Those are different judgements made on different evidence — a
    name can pre-rank fifteenth on price action and come out of deep analysis
    with the strongest case on the desk.

    Separated from the regression above because there the two orders agree, so
    that test would pass even if the final sort were dropped entirely.
    """
    probe, _ = await _run(monkeypatch, desk, count=200)
    finalists = probe.shortlist[:20]

    # Conviction deliberately runs opposite to the quant ranking: the name that
    # pre-ranked last is the one deep analysis liked most.
    confidences = {sym: 0.10 + i / 100 for i, sym in enumerate(finalists)}
    result, _ = await _run(monkeypatch, desk, count=200, confidences=confidences)

    assert result.published == list(reversed(finalists))[:5], (
        f"published in shortlist order rather than by conviction: {result.published}"
    )


@pytest.mark.asyncio
async def test_randomised_completion_order_gives_the_same_ranking(
    monkeypatch: pytest.MonkeyPatch, desk: Desk
) -> None:
    """Same data, different arrival times, identical output.

    The feed delays each answer by a random amount, so workers finish in a
    different order on every seed. Anything that leaked completion order into
    the result — a shortlist built by appending, a ranking without a total
    ordering — shows up here and nowhere else.
    """
    baseline, _ = await _run(monkeypatch, desk, count=200)
    orders = [baseline.published]
    shortlists = [baseline.shortlist]

    for seed in (1, 7, 13, 99):
        run, _ = await _run(monkeypatch, desk, count=200, shuffle_seed=seed)
        orders.append(run.published)
        shortlists.append(run.shortlist)

    assert all(o == orders[0] for o in orders), f"ranking moved with arrival order: {orders}"
    assert all(s == shortlists[0] for s in shortlists), "the shortlist moved with arrival order"


@pytest.mark.asyncio
async def test_the_funnel_balances_over_a_thousand_names(
    monkeypatch: pytest.MonkeyPatch, desk: Desk
) -> None:
    """Every instrument lands in exactly one terminal bucket.

    The accounting invariant. A candidate that disappears silently is how a desk
    ends up unable to explain why a name it expected was never proposed.
    """
    result, _feed = await _run(monkeypatch, desk)
    f = result.funnel

    assert f.reconciles(), (
        f"{f.unaccounted()} of {f.universe_total} instruments went unaccounted for; "
        f"terminal total {f.terminal_total()}"
    )


@pytest.mark.asyncio
async def test_the_funnel_still_balances_when_things_go_wrong(
    monkeypatch: pytest.MonkeyPatch, desk: Desk
) -> None:
    """A clean run balances trivially; the ledger has to balance on a bad day.

    Failures are where accounting is actually lost, because the exception path
    is the one that returns early. Here a third of the shortlist raises in deep
    analysis and the budget refuses the rest, and every one of them must still
    land in a bucket.
    """
    settings = scanner_settings(
        TRAIDO_MARKET_PREFILTER_LIMIT=150,
        TRAIDO_QUANT_TOP_K=30,
        TRAIDO_DEEP_ANALYSIS_TOP_K=12,
        TRAIDO_MAX_LLM_CANDIDATES=8,
    )
    ctx = fake_scan_context(settings, market_data=FakeMarketData())
    seen: list[str] = []

    async def _pipeline(symbol: str, **_kw: object) -> PipelineResult:
        seen.append(symbol)
        if len(seen) % 3 == 0:
            raise RuntimeError("market data down")
        if len(seen) % 3 == 1:
            return PipelineResult(pipeline_run_id=uuid4(), symbol=symbol, status="no_candidate")
        return _passed(symbol, confidence=0.5)

    monkeypatch.setattr(scan_cycle, "run_symbol_pipeline", _pipeline)

    result = await run_cycle(
        settings=settings,
        universe_service=_service(300),
        timeframes=(),
        max_open=2,
        context=ctx,
    )
    f = result.funnel

    assert f.deep_analysis_failed > 0, "the failing symbols were not exercised"
    assert f.ai_budget_exhausted > 0, "the budget was not exercised"
    assert f.reconciles(), (
        f"{f.unaccounted()} of {f.universe_total} instruments went unaccounted for; "
        f"terminal total {f.terminal_total()}"
    )


@pytest.mark.asyncio
async def test_the_broker_account_is_read_once_for_the_whole_universe(
    monkeypatch: pytest.MonkeyPatch, desk: Desk
) -> None:
    """One portfolio read per cycle, not one per symbol.

    The scan context has always promised this; at sixty names a regression would
    have cost sixty requests, at a thousand it is a thousand, and the promise is
    only checkable at a size where breaking it is obvious.
    """
    resolved = scanner_settings(
        TRAIDO_MARKET_PREFILTER_LIMIT=150,
        TRAIDO_QUANT_TOP_K=30,
        TRAIDO_DEEP_ANALYSIS_TOP_K=20,
        TRAIDO_MAX_LLM_CANDIDATES=20,
    )
    ctx = fake_scan_context(resolved, market_data=FakeMarketData())

    async def _pipeline(symbol: str, **_kw: object) -> PipelineResult:
        await ctx.portfolio()
        return _passed(symbol, confidence=0.5)

    monkeypatch.setattr(scan_cycle, "run_symbol_pipeline", _pipeline)

    await run_cycle(
        settings=resolved,
        universe_service=_service(UNIVERSE_SIZE),
        timeframes=(),
        max_open=5,
        context=ctx,
    )

    assert ctx.broker.portfolio_calls == 1


@pytest.mark.asyncio
async def test_capacity_is_spent_after_ranking_not_during_analysis(
    monkeypatch: pytest.MonkeyPatch, desk: Desk
) -> None:
    """The five best of the shortlist, not the first five to be analysed.

    Reserving a slot when a candidate passes risk — which is what publishing
    inside the loop amounts to — hands the desk whichever names finished first.
    Under concurrency that is not even the alphabetical order it used to be.
    """
    # Give the *last* names of the shortlist the highest confidence, so the
    # first five analysed are the weakest.
    result_probe, _ = await _run(monkeypatch, desk, count=200)
    # The finalists, not the whole shortlist: only the first `deep_analysis_top_k`
    # of Stage 2's ranking reach Stage 3, so a name past that point never
    # produces a candidate to rank and could not have won a slot anyway.
    finalists = result_probe.shortlist[:20]
    assert len(finalists) == 20

    strong = {sym: 0.90 + i / 1000 for i, sym in enumerate(reversed(finalists[-5:]))}
    weak = {sym: 0.10 for sym in finalists[:-5]}

    result, _ = await _run(monkeypatch, desk, count=200, confidences={**weak, **strong})

    assert set(result.published) == set(strong), (
        f"capacity went to the wrong candidates: {result.published}"
    )


@pytest.mark.asyncio
async def test_an_exhausted_ai_budget_shortens_the_list_deterministically(
    monkeypatch: pytest.MonkeyPatch, desk: Desk
) -> None:
    """A budget of three analyses the three best, and says so.

    Not a random three, and not the three that happened to be ready — the top of
    the same deterministic pre-ranking the unbudgeted run would have used.
    """
    settings = scanner_settings(
        TRAIDO_MARKET_PREFILTER_LIMIT=150,
        TRAIDO_QUANT_TOP_K=30,
        TRAIDO_DEEP_ANALYSIS_TOP_K=20,
        TRAIDO_MAX_LLM_CANDIDATES=3,
    )
    analysed: list[str] = []
    result, _ = await _run(monkeypatch, desk, count=200, settings=settings, analysed=analysed)

    assert len(analysed) == 3
    assert sorted(analysed) == sorted(result.shortlist[:3])
    assert result.funnel.ai_budget_exhausted == 17, "refused candidates must still be counted"
    assert result.funnel.reconciles()


@pytest.mark.asyncio
async def test_concurrency_stays_within_its_budget(
    monkeypatch: pytest.MonkeyPatch, desk: Desk
) -> None:
    """Never more workers in flight than the budget allows.

    `asyncio.gather` over a thousand symbols is the failure this rules out: it
    is not slower, it is a burst that gets the account throttled for the rest of
    the session, and every fail-closed gate downstream then reads a 429 as a
    vendor outage.
    """
    settings = scanner_settings(
        TRAIDO_MARKET_PREFILTER_LIMIT=150,
        TRAIDO_QUANT_TOP_K=30,
        TRAIDO_DEEP_ANALYSIS_TOP_K=20,
        TRAIDO_MAX_LLM_CANDIDATES=20,
        TRAIDO_SCANNER_CONCURRENCY=3,
    )
    ctx = fake_scan_context(settings, market_data=FakeMarketData())
    import asyncio

    in_flight = 0
    peak = 0

    async def _pipeline(symbol: str, **_kw: object) -> PipelineResult:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.001)
        in_flight -= 1
        return _passed(symbol, confidence=0.5)

    monkeypatch.setattr(scan_cycle, "run_symbol_pipeline", _pipeline)
    from core.concurrency import ConcurrencyManager
    from tests.scanner_fakes import unpaced_budgets

    # The semaphore alone, with the rate limiter switched off. With pacing left
    # in, `deep` releases two tokens a second and the workers finish in a
    # millisecond, so only one is ever in flight and the measured peak is 1
    # however wide the semaphore is — the test would pass with no bound at all.
    ctx.concurrency = ConcurrencyManager(unpaced_budgets(settings.scanner_concurrency))

    await run_cycle(
        settings=settings,
        universe_service=_service(300),
        timeframes=(),
        max_open=5,
        context=ctx,
    )

    assert 0 < peak <= 3, f"peak concurrency {peak} exceeded the budget of 3"


@pytest.mark.asyncio
async def test_one_symbol_failing_does_not_kill_the_scan(
    monkeypatch: pytest.MonkeyPatch, desk: Desk
) -> None:
    """A provider error for one name is recorded, and the rest still publish."""
    settings = scanner_settings(
        TRAIDO_MARKET_PREFILTER_LIMIT=150,
        TRAIDO_QUANT_TOP_K=30,
        TRAIDO_DEEP_ANALYSIS_TOP_K=20,
        TRAIDO_MAX_LLM_CANDIDATES=20,
    )
    ctx = fake_scan_context(settings, market_data=FakeMarketData())
    doomed: list[str] = []

    async def _pipeline(symbol: str, **_kw: object) -> PipelineResult:
        if not doomed:
            doomed.append(symbol)
        if symbol == doomed[0]:
            raise RuntimeError("market data down")
        return _passed(symbol, confidence=0.5)

    monkeypatch.setattr(scan_cycle, "run_symbol_pipeline", _pipeline)

    result = await run_cycle(
        settings=settings,
        universe_service=_service(200),
        timeframes=(),
        max_open=5,
        context=ctx,
    )

    assert result.funnel.deep_analysis_failed == 1
    assert len(result.published) == 5, "one bad symbol stopped the cycle"
    assert result.funnel.reconciles()
