"""The desk's few slots go to the best of the universe, not the first to qualify.

Roughly one symbol in five clears strategy and risk, so a cap of five proposals
is reached about a third of the way through a sixty-name universe. Publishing as
you go therefore never shows the operator the strongest ideas — only the
earliest alphabetically placed ones that happened to pass.

Ranking is only honest if the whole pass happens before anything is offered, and
only safe if a proposal already on the desk is never taken back *by ranking*:
displacing a card to make room for a better one races with the very click it is
waiting for. The one thing that may take a card down is `withdraw_unactionable`,
and only because those cards cannot be clicked at all — see
`test_stale_proposals_are_taken_down.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from agents.scanner import agent as scanner
from agents.scanner import cycle as scan_cycle
from core.enums import OpportunityStatus, RiskVerdict, TradeAction, TradingMode
from core.schemas import (
    PipelineResult,
    PortfolioSnapshot,
    RiskDecision,
    TradeCandidate,
    TradeOpportunity,
)
from risk.limits import default_risk_limits
from tests.scanner_fakes import fake_scan_context, scanner_settings, universe_service_for


@pytest.fixture(autouse=True)
def quiet_scanner(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = scanner_settings()
    monkeypatch.setattr(scanner.BOARD, "log", lambda *a, **k: None)
    monkeypatch.setattr(scanner.BOARD, "set_agent", lambda *a, **k: None)
    monkeypatch.setattr(scan_cycle.BOARD, "log", lambda *a, **k: None)
    monkeypatch.setattr(scan_cycle.BOARD, "set_agent", lambda *a, **k: None)
    monkeypatch.setattr(scanner, "is_kill_switch_on", lambda: False)
    monkeypatch.setattr(scanner, "get_settings", lambda: settings)

    # Stage 1 and Stage 2 read real market data now, so the cycle needs vendors
    # that answer rather than a context that yields nothing. They are
    # deterministic, so what they answer cannot influence the ordering under
    # test — every symbol below clears the cheap stages and reaches the faked
    # pipeline, which is what decides the ranking.
    monkeypatch.setattr(
        scan_cycle, "open_scan_context", lambda _s=None, **_kw: fake_scan_context(settings)
    )
    monkeypatch.setattr(scanner, "_wake_token", 0)
    monkeypatch.setattr(scanner, "_wake_seen", 0)
    scanner.STATUS.cycle = 0
    scanner.STATUS.error = None


def _candidate(symbol: str, *, confidence: float, risk_reward: float = 2.0) -> TradeCandidate:
    return TradeCandidate(
        symbol=symbol,
        action=TradeAction.BUY,
        confidence=confidence,
        entry=Decimal(100),
        stop=Decimal(95),
        target=Decimal(110),
        risk_reward=risk_reward,
        reasons=["test"],
        strategy_version="test@1",
    )


def _risk_pass() -> RiskDecision:
    return RiskDecision(
        verdict=RiskVerdict.PASS,
        sized_qty=Decimal(10),
        limits_applied=default_risk_limits(),
        portfolio=PortfolioSnapshot(
            equity=Decimal(100000),
            cash=Decimal(100000),
            buying_power=Decimal(100000),
            open_exposure=Decimal(0),
            day_pnl=Decimal(0),
            week_pnl=Decimal(0),
            drawdown_pct=0.0,
            open_positions=0,
        ),
    )


def _passed(symbol: str, *, confidence: float, risk_reward: float = 2.0) -> PipelineResult:
    return PipelineResult(
        pipeline_run_id=uuid4(),
        symbol=symbol,
        status="risk_passed",
        candidate=_candidate(symbol, confidence=confidence, risk_reward=risk_reward),
        risk=_risk_pass(),
    )


def test_ranking_puts_the_most_confident_first() -> None:
    weak = _passed("A", confidence=0.4)
    strong = _passed("B", confidence=0.9)

    ordered = sorted([weak, strong], key=scanner.rank_key)

    assert [r.symbol for r in ordered] == ["B", "A"]


def test_risk_reward_only_breaks_a_tie() -> None:
    """Geometry decides between equals; on its own it would reward distant targets."""
    tight = _passed("A", confidence=0.7, risk_reward=1.5)
    wide = _passed("B", confidence=0.7, risk_reward=4.0)
    confident_but_tight = _passed("C", confidence=0.8, risk_reward=1.1)

    ordered = sorted([tight, wide, confident_but_tight], key=scanner.rank_key)

    assert [r.symbol for r in ordered] == ["C", "B", "A"]


def test_two_identical_candidates_are_still_totally_ordered() -> None:
    """The symbol is the last tie-break, and it is what makes the order total.

    Presented shuffled, because a stable sort over equal keys preserves input
    order and would therefore hide a missing tie-break for as long as the input
    order happens to be deterministic. It is today — results come back
    positionally — but that is a property of one call site, not a guarantee, and
    the day it changes the desk would start proposing a different name for the
    same market with nothing in the diff to explain it.
    """
    twins = [_passed(sym, confidence=0.7, risk_reward=2.0) for sym in ("C", "A", "B")]

    from itertools import permutations

    orders = {tuple(r.symbol for r in sorted(p, key=scanner.rank_key)) for p in permutations(twins)}

    assert orders == {("A", "B", "C")}


def _standing_cards(n: int) -> list[TradeOpportunity]:
    """Real cards, because the cycle now reads them and not only counts them.

    These used to be `[None] * n`, which was fair while the only question asked
    of the queue was its length. `withdraw_unactionable` inspects each card, so
    a sentinel would exercise the sweep's error path instead of the sweep.
    Fresh and on symbols nothing holds, so a healthy sweep leaves them alone.
    """
    now = datetime.now(UTC)
    return [
        TradeOpportunity(
            id=uuid4(),
            candidate=_candidate(f"STAND{i}", confidence=0.5),
            risk=_risk_pass(),
            status=OpportunityStatus.AWAITING_CONFIRMATION,
            trading_mode=TradingMode.CONFIRMATION,
            created_at=now,
            expires_at=now + timedelta(minutes=60),
        )
        for i in range(n)
    ]


def _install_scan(
    monkeypatch: pytest.MonkeyPatch,
    results: dict[str, PipelineResult],
    *,
    open_proposals: int = 0,
    max_open: int = 2,
) -> list[str]:
    """Run a cycle over `results` and return the symbols actually published."""
    cfg = {
        "universe": list(results),
        "timeframes": ["1d"],
        "max_open_buy_opportunities": max_open,
        "enabled": True,
    }
    monkeypatch.setattr(scanner, "load_watchlist", lambda: cfg)
    monkeypatch.setattr(
        scanner, "universe_service", lambda _s=None: universe_service_for(list(results))
    )
    # The queue has to grow as cards are written, because capacity is now
    # re-read immediately before each publication rather than sliced once up
    # front. A stub that answered the same count forever would let a cycle
    # publish past a cap the real store would have closed.
    queue: list[TradeOpportunity] = _standing_cards(open_proposals)
    monkeypatch.setattr(scan_cycle.OPPORTUNITIES, "list_open", lambda: list(queue))
    monkeypatch.setattr(scan_cycle, "withdraw_unactionable", lambda *_a, **_kw: 0)

    async def _pipeline(symbol: str, **kwargs: object) -> PipelineResult:
        assert kwargs.get("publish") is False, "a ranking pass must not publish as it goes"
        return results[symbol]

    monkeypatch.setattr(scan_cycle, "run_symbol_pipeline", _pipeline)

    published: list[str] = []

    async def _publish(result: PipelineResult, _risk: object, **_kw: object) -> PipelineResult:
        published.append(result.symbol)
        queue.extend(_standing_cards(1))
        return result.model_copy(
            update={"status": "awaiting_confirmation", "opportunity": object()}
        )

    monkeypatch.setattr(scan_cycle, "publish_opportunity", _publish)
    return published


@pytest.mark.asyncio
async def test_nothing_is_offered_before_the_whole_pass_is_done(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strongest name may be last in the universe; publishing early misses it."""
    results = {
        "A": _passed("A", confidence=0.3),
        "B": _passed("B", confidence=0.5),
        "Z": _passed("Z", confidence=0.95),
    }
    published = _install_scan(monkeypatch, results, max_open=1)

    await scanner.run_scan_cycle()

    assert published == ["Z"]


@pytest.mark.asyncio
async def test_only_the_free_slots_are_filled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proposal already awaiting a decision keeps its place."""
    results = {
        "A": _passed("A", confidence=0.9),
        "B": _passed("B", confidence=0.8),
        "C": _passed("C", confidence=0.7),
    }
    published = _install_scan(monkeypatch, results, open_proposals=2, max_open=3)

    await scanner.run_scan_cycle()

    assert published == ["A"], "the pass took more than the one free slot"


@pytest.mark.asyncio
async def test_a_full_desk_is_not_scanned_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full pass costs a pipeline run per symbol and can win nothing."""
    results = {"A": _passed("A", confidence=0.9)}
    published = _install_scan(monkeypatch, results, open_proposals=3, max_open=3)

    status = await scanner.run_scan_cycle()

    assert published == []
    assert status.funnel.deep_analysis_started == 0
    assert status.funnel.paused_on_full_queue is True


@pytest.mark.asyncio
async def test_the_funnel_accounts_for_everyone_who_passed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "3 candidates, 1 proposal" must not leave the other two unexplained.

    Selection and failure look identical in a funnel that only counts the
    output, which is the same blindness that made a paused cycle read as an
    empty market.
    """
    results = {
        "A": _passed("A", confidence=0.9),
        "B": _passed("B", confidence=0.8),
        "C": _passed("C", confidence=0.7),
    }
    _install_scan(monkeypatch, results, max_open=1)

    status = await scanner.run_scan_cycle()

    assert status.funnel.risk_passed == 3
    assert status.funnel.published == 1
    assert status.funnel.final_outranked == 2


@pytest.mark.asyncio
async def test_a_pass_with_no_survivors_offers_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty market must still look different from a full queue."""
    rejected = PipelineResult(
        pipeline_run_id=uuid4(),
        symbol="A",
        status="risk_rejected",
        candidate=_candidate("A", confidence=0.9),
        risk=_risk_pass().model_copy(update={"verdict": RiskVerdict.REJECT}),
    )
    published = _install_scan(monkeypatch, {"A": rejected}, max_open=3)

    status = await scanner.run_scan_cycle()

    assert published == []
    assert status.funnel.deep_analysis_started == 1
    assert status.funnel.paused_on_full_queue is False
    assert status.funnel.final_outranked == 0
