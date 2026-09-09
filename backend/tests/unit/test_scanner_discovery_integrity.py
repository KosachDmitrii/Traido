"""Production-shaped budgets, durable rotation and broker-only exposure."""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from agents.scanner import cycle
from agents.scanner.funnel import ScanFunnel
from agents.scanner.rotation import load_history, record_selection
from core.enums import Timeframe
from tests.scanner_fakes import (
    FakeUniverseProvider,
    fake_scan_context,
    scanner_settings,
    universe_service_for,
)
from universe.models import UniverseTier
from universe.service import UniverseService


@pytest.mark.asyncio
async def test_rotation_reaches_beyond_2000_with_cached_reference_and_restart():
    provider = FakeUniverseProvider(12500, otc_every=11, inactive_every=17)
    history = {}
    observed = set()
    first = None
    for turn in range(14):
        if turn in (0, 7):
            service = UniverseService(provider)
        snapshot = await service.get_scan_universe(
            tier=UniverseTier.BROAD,
            max_size=2000,
            last_seen=history.get("universe_selected"),
        )
        assert len(snapshot.eligible) == 2000
        assert all(i.active and not i.otc for i in snapshot.eligible)
        if first is None:
            first = set(snapshot.symbols)
        observed.update(snapshot.symbols)
        record_selection("universe_selected", snapshot.symbols)
        history = load_history()
    full = await service.get_universe(tier=UniverseTier.BROAD, max_size=0)
    assert observed == set(full.symbols)
    assert len(observed - first) > 8000
    assert provider.calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_broker_only_position_or_unreadable_account_never_reaches_deep(
    monkeypatch, unavailable
):
    settings = scanner_settings()
    ctx = fake_scan_context(settings)
    seen = []

    async def positions():
        if unavailable:
            raise ConnectionError("offline")
        return [SimpleNamespace(symbol="BAC", qty=Decimal(793))]

    async def analyse(symbol, **kwargs):
        seen.append(symbol)
        return SimpleNamespace(status="no_candidate", candidate=None, risk=None, errors=[])

    monkeypatch.setattr(ctx.broker, "list_positions", positions)
    monkeypatch.setattr(cycle, "run_symbol_pipeline", analyse)
    monkeypatch.setattr(cycle, "withdraw_unactionable", lambda *a: None)
    result = await cycle.run_cycle(
        settings=settings,
        universe_service=universe_service_for(["BAC", "WMT"]),
        timeframes=(Timeframe.D1,),
        max_open=5,
        context=ctx,
    )
    assert "BAC" not in seen
    assert result.funnel.reconciles()
    if unavailable:
        assert seen == []
        assert result.funnel.operational_blocked == 2
        assert result.error == "broker_positions_unavailable"
    else:
        assert seen == ["WMT"]
        assert result.funnel.position_open == 1


@pytest.mark.parametrize(
    "status,field",
    [("data_blocked", "data_blocked"), ("operational_blocked", "operational_blocked")],
)
def test_missing_candidate_does_not_hide_service_or_data_failure(status, field):
    funnel = ScanFunnel(universe_total=1)
    outcome = SimpleNamespace(status=status, candidate=None, errors=["UNAVAILABLE"], risk=None)
    cycle._record_deep_outcome(outcome, funnel, [])
    assert getattr(funnel, field) == 1
    assert funnel.deep_analysis_no_candidate == 0
    assert funnel.rejection_reasons == {"UNAVAILABLE": 1}
    assert funnel.reconciles()


def test_risk_reasons_are_counted_once_per_symbol():
    funnel = ScanFunnel(universe_total=1)
    outcome = SimpleNamespace(
        status="risk_rejected",
        candidate=object(),
        errors=["WEEKLY_PNL_UNAVAILABLE"],
        risk=SimpleNamespace(reasons=["WEEKLY_PNL_UNAVAILABLE", "PORTFOLIO_DRAWDOWN_UNAVAILABLE"]),
    )
    cycle._record_deep_outcome(outcome, funnel, [])
    assert funnel.risk_rejected == 1
    assert funnel.rejection_reasons["WEEKLY_PNL_UNAVAILABLE"] == 1
    assert funnel.reconciles()


@pytest.mark.asyncio
async def test_fill_during_analysis_is_excluded_before_publication(monkeypatch):
    from tests.unit.test_scanner_ranking import _passed

    ctx = fake_scan_context()
    reads = 0

    async def positions():
        nonlocal reads
        reads += 1
        return [] if reads == 1 else [SimpleNamespace(symbol="BAC", qty=Decimal(793))]

    async def analyse(symbol, **kwargs):
        return _passed(symbol, confidence=0.8)

    async def unexpected_publish(*args, **kwargs):
        pytest.fail("broker-held symbol must not become a BUY card")

    monkeypatch.setattr(ctx.broker, "list_positions", positions)
    monkeypatch.setattr(cycle, "run_symbol_pipeline", analyse)
    monkeypatch.setattr(cycle, "publish_opportunity", unexpected_publish)
    monkeypatch.setattr(cycle, "withdraw_unactionable", lambda *a: None)
    result = await cycle.run_cycle(
        settings=ctx.settings,
        universe_service=universe_service_for(["BAC"]),
        timeframes=(Timeframe.D1,),
        max_open=5,
        context=ctx,
    )
    assert reads == 2
    assert result.funnel.position_open == 1
    assert result.published == []
    assert result.funnel.reconciles()


@pytest.mark.asyncio
async def test_completed_outcomes_are_visible_before_cycle_finishes(monkeypatch):
    from core.concurrency import ConcurrencyManager
    from tests.scanner_fakes import unpaced_budgets

    ctx = fake_scan_context()
    ctx.concurrency = ConcurrencyManager(unpaced_budgets(1))
    progress = []
    completed_seen = []

    async def analyse(symbol, **kwargs):
        completed_seen.append(progress[0].deep_analysis_completed)
        return SimpleNamespace(status="no_candidate", candidate=None, risk=None, errors=[])

    monkeypatch.setattr(cycle, "run_symbol_pipeline", analyse)
    monkeypatch.setattr(cycle, "withdraw_unactionable", lambda *a: None)
    result = await cycle.run_cycle(
        settings=ctx.settings,
        universe_service=universe_service_for(["BAC", "WMT"]),
        timeframes=(Timeframe.D1,),
        max_open=5,
        context=ctx,
        on_progress=progress.append,
    )
    assert completed_seen == [0, 1]
    assert result.funnel.deep_analysis_completed == 2
    assert result.funnel.reconciles()
