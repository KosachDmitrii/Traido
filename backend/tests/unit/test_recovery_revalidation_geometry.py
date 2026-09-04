"""Restart/recovery must not publish stale admission against a new quote."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from core.enums import (
    AdmissionDecision,
    EntryWatchStatus,
    InstrumentThesis,
    RiskVerdict,
    SetupType,
    TradeAction,
)
from core.schemas import (
    AdmissionSnapshot,
    EntryWatch,
    PipelineResult,
    Quote,
    TradeAdmissionResult,
    TradeCandidate,
)
from trading.entry_watch_loop import (
    _convert_admitted_watch,
    _defer_to_recovery_revalidation,
    run_watch_pass,
)
from trading.entry_watches import ENTRY_WATCHES, admission_claim_key


def _quote(*, ask: str) -> Quote:
    return Quote(
        symbol="XOM",
        bid=Decimal(str(float(ask) - 0.05)),
        ask=Decimal(ask),
        ts=datetime.now(UTC),
        source="test",
    )


def _admitted_watch(*, entry: float = 100.05, trigger_version: int = 1) -> EntryWatch:
    now = datetime.now(UTC)
    cand = TradeCandidate(
        symbol="XOM",
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal(str(entry)),
        stop=Decimal("98.5"),
        target=Decimal(105),
        risk_reward=2.0,
        reasons=["base"],
        strategy_version="test",
        thesis=InstrumentThesis.BULLISH,
    )
    watch = EntryWatch(
        id=uuid4(),
        symbol="XOM",
        strategy_version="test",
        exec_timeframe="H1",
        created_at=now,
        valid_until=now + timedelta(hours=2),
        thesis=InstrumentThesis.BULLISH,
        signal_price=Decimal(100),
        current_price_at_creation=Decimal(100),
        last_price=Decimal(str(entry)),
        entry_zone_low=Decimal("99.5"),
        entry_zone_high=Decimal("100.5"),
        planned_entry=Decimal(100),
        planned_stop=Decimal("98.5"),
        planned_target=Decimal(105),
        entry_quality_at_creation=70,
        status=EntryWatchStatus.ADMITTED,
        trigger_version=trigger_version,
        reasons=[],
        candidate=cand,
        last_admission_record_id=uuid4(),
    )
    ENTRY_WATCHES.clear()
    ENTRY_WATCHES.update(watch)
    return watch


def _fresh_admission(*, entry: float = 100.05, target: float = 105.0) -> TradeAdmissionResult:
    snap = AdmissionSnapshot(
        price_at_creation=entry,
        atr_at_creation=1.0,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality_at_creation=70,
        entry_quality_at_creation=70,
        stop_at_creation=98.5,
        target_at_creation=target,
        entry_at_creation=entry,
        effective_rr_at_creation=2.0,
    )
    return TradeAdmissionResult(
        decision=AdmissionDecision.BUY_ALLOWED,
        admitted=True,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality=70,
        entry_quality=70,
        effective_rr=2.0,
        chase_score=10,
        structure_valid=True,
        stop_valid=True,
        target_valid=True,
        reason_codes=["BUY_ALLOWED"],
        snapshot=snap,
    )


@pytest.fixture(autouse=True)
def _clean_watches() -> None:
    ENTRY_WATCHES.clear()
    yield
    ENTRY_WATCHES.clear()


def test_defer_recovery_moves_admitted_to_triggered() -> None:
    watch = _admitted_watch()
    stats = {"still_waiting": 0, "invalidated": 0, "converted": 0}
    _defer_to_recovery_revalidation(watch, stats=stats)
    updated = ENTRY_WATCHES.get(watch.id)
    assert updated is not None
    assert updated.status is EntryWatchStatus.TRIGGERED
    assert any("RECOVERY_REVALIDATION_REQUIRED" in r for r in updated.reasons)
    assert stats["still_waiting"] == 1


@pytest.mark.asyncio
async def test_recovery_convert_without_fresh_admission_publishes_nothing() -> None:
    watch = _admitted_watch(entry=100.05)
    stats = {
        "checked": 0,
        "triggered": 0,
        "converted": 0,
        "invalidated": 0,
        "still_waiting": 0,
    }
    publish_mock = AsyncMock()
    auto_mock = AsyncMock(return_value=False)

    with (
        patch("trading.entry_watch_loop.publish_opportunity", publish_mock),
        patch("trading.auto_trigger_policy.maybe_auto_approve_opportunity", auto_mock),
    ):
        await _convert_admitted_watch(
            watch,
            md=AsyncMock(),
            settings=MagicMock(fred_api_key=None, finnhub_api_key=None),
            audit=AsyncMock(),
            stats=stats,
            price=101.0,
            quote=_quote(ask="101.00"),
            admission=None,
        )

    publish_mock.assert_not_called()
    auto_mock.assert_not_called()
    updated = ENTRY_WATCHES.get(watch.id)
    assert updated is not None
    assert updated.status is EntryWatchStatus.TRIGGERED
    assert any("RECOVERY_REVALIDATION_REQUIRED" in r for r in updated.reasons)
    assert stats["converted"] == 0


@pytest.mark.asyncio
async def test_fresh_admission_same_pass_can_publish_with_matching_geometry() -> None:
    watch = _admitted_watch(entry=100.05)
    stats = {
        "checked": 0,
        "triggered": 0,
        "converted": 0,
        "invalidated": 0,
        "still_waiting": 0,
    }
    admission = _fresh_admission(entry=100.05, target=105.0)
    publish_mock = AsyncMock(
        return_value=PipelineResult(
            pipeline_run_id=uuid4(),
            symbol="XOM",
            status="completed",
            opportunity=None,
        )
    )
    auto_mock = AsyncMock(return_value=False)
    risk_result = MagicMock(verdict=RiskVerdict.PASS, sized_qty=10, reasons=[])

    with (
        patch("trading.entry_watch_loop.open_scan_context") as scan_ctx,
        patch("trading.entry_watch_loop.publish_opportunity", publish_mock),
        patch("trading.auto_trigger_policy.maybe_auto_approve_opportunity", auto_mock),
        patch("agents.market.agent.assess_market", new_callable=AsyncMock),
        patch("trading.market_gate.evaluate_market_gate_for_candidate") as gate,
        patch("trading.entry_watch_loop.build_risk_context", new_callable=AsyncMock) as brc,
        patch("trading.entry_watch_loop.RiskEngine") as risk_engine,
        patch("trading.entry_watch_loop.default_risk_limits", return_value=object()),
    ):
        gate.return_value.tradable_long = True
        gate.return_value.reason_codes = []
        brc.return_value = MagicMock(context=object())
        risk_engine.return_value.evaluate.return_value = risk_result

        class _Ctx:
            broker = object()
            market_data = object()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def portfolio(self):
                return object()

        scan_ctx.return_value = _Ctx()
        await _convert_admitted_watch(
            watch,
            md=AsyncMock(),
            settings=MagicMock(fred_api_key=None, finnhub_api_key=None),
            audit=AsyncMock(),
            stats=stats,
            price=100.05,
            quote=_quote(ask="100.05"),
            admission=admission,
        )

    publish_mock.assert_called_once()
    auto_mock.assert_not_called()


@pytest.mark.asyncio
async def test_watch_pass_recovery_defers_stale_admitted_watch() -> None:
    watch = _admitted_watch(entry=100.05)
    publish_mock = AsyncMock()
    auto_mock = AsyncMock(return_value=False)

    with (
        patch(
            "trading.watch_marks.refresh_all_watch_marks",
            new_callable=AsyncMock,
            return_value={watch.symbol: (101.0, _quote(ask="101.00"))},
        ),
        patch("trading.entry_watch_loop.publish_opportunity", publish_mock),
        patch("trading.auto_trigger_policy.maybe_auto_approve_opportunity", auto_mock),
        patch("trading.entry_watch_loop.ensure_seeded_from_aftermath", return_value=0),
        patch("trading.shadow_outcomes.SHADOW_OUTCOMES.finalize_expired"),
    ):
        stats = await run_watch_pass()

    publish_mock.assert_not_called()
    auto_mock.assert_not_called()
    assert stats["converted"] == 0
    updated = ENTRY_WATCHES.get(watch.id)
    assert updated is not None
    assert updated.status is EntryWatchStatus.TRIGGERED
    assert any("RECOVERY_REVALIDATION_REQUIRED" in r for r in updated.reasons)


@pytest.mark.asyncio
async def test_stale_recovery_never_auto_triggers_even_when_enabled() -> None:
    watch = _admitted_watch(entry=100.05)
    auto_mock = AsyncMock(return_value=True)

    with (
        patch("trading.entry_watch_loop.publish_opportunity", AsyncMock()),
        patch("trading.auto_trigger_policy.maybe_auto_approve_opportunity", auto_mock),
    ):
        await _convert_admitted_watch(
            watch,
            md=AsyncMock(),
            settings=MagicMock(fred_api_key=None, finnhub_api_key=None),
            audit=AsyncMock(),
            stats={"converted": 0, "invalidated": 0, "still_waiting": 0},
            price=101.0,
            quote=_quote(ask="101.00"),
            admission=None,
        )

    auto_mock.assert_not_called()


def test_recovery_releases_stuck_admission_claim_after_converting_crash() -> None:
    """Same-process CONVERTING crash must not block the next conversion attempt."""
    watch = _admitted_watch(entry=100.05, trigger_version=1)
    key = admission_claim_key(watch.id, watch.trigger_version)
    assert ENTRY_WATCHES.claim_admission(key) is True

    converting = ENTRY_WATCHES.mark(
        watch.id, EntryWatchStatus.CONVERTING, reason="CONVERSION_CLAIM"
    )
    assert converting is not None
    assert converting.status is EntryWatchStatus.CONVERTING

    stats = {"still_waiting": 0, "converted": 0, "invalidated": 0}
    _defer_to_recovery_revalidation(converting, stats=stats)

    updated = ENTRY_WATCHES.get(watch.id)
    assert updated is not None
    assert updated.status is EntryWatchStatus.TRIGGERED
    assert updated.trigger_version == 1
    assert ENTRY_WATCHES.claim_admission(key) is True


def test_published_opportunity_keeps_admission_claim() -> None:
    watch = _admitted_watch(trigger_version=2)
    key = admission_claim_key(watch.id, watch.trigger_version)
    assert ENTRY_WATCHES.claim_admission(key) is True

    opp_id = uuid4()
    published = ENTRY_WATCHES.update(watch.model_copy(update={"converted_opportunity_id": opp_id}))
    assert ENTRY_WATCHES.release_admission_if_unpublished(published) is False
    assert ENTRY_WATCHES.claim_admission(key) is False


@pytest.mark.asyncio
async def test_watch_pass_recovers_converting_and_releases_admission_claim() -> None:
    """CONVERTING leftover from a same-process crash must reclaim on the next pass."""
    watch = _admitted_watch(entry=100.05, trigger_version=1)
    key = admission_claim_key(watch.id, watch.trigger_version)
    assert ENTRY_WATCHES.claim_admission(key) is True
    converting = ENTRY_WATCHES.mark(
        watch.id, EntryWatchStatus.CONVERTING, reason="CONVERSION_CLAIM"
    )
    assert converting is not None

    with (
        patch(
            "trading.watch_marks.refresh_all_watch_marks",
            new_callable=AsyncMock,
            return_value={watch.symbol: (100.05, _quote(ask="100.05"))},
        ),
        patch("trading.entry_watch_loop.publish_opportunity", AsyncMock()),
        patch("trading.auto_trigger_policy.maybe_auto_approve_opportunity", AsyncMock()),
        patch("trading.entry_watch_loop.ensure_seeded_from_aftermath", return_value=0),
        patch("trading.shadow_outcomes.SHADOW_OUTCOMES.finalize_expired"),
    ):
        stats = await run_watch_pass()

    assert stats["converted"] == 0
    updated = ENTRY_WATCHES.get(watch.id)
    assert updated is not None
    assert updated.status is EntryWatchStatus.TRIGGERED
    assert updated.trigger_version == 1
    assert ENTRY_WATCHES.claim_admission(key) is True


@pytest.mark.asyncio
async def test_convert_exception_releases_unpublished_admission_claim() -> None:
    """Crash after claim, before publish — same-process reclaim must succeed."""
    watch = _admitted_watch(entry=100.05, trigger_version=1)
    key = admission_claim_key(watch.id, watch.trigger_version)
    admission = _fresh_admission(entry=100.05, target=105.0)
    stats = {"converted": 0, "invalidated": 0, "still_waiting": 0}

    with (
        patch(
            "trading.entry_watch_loop.build_candidate_from_revalidation",
            side_effect=RuntimeError("boom"),
        ),
        pytest.raises(RuntimeError, match="boom"),
    ):
        await _convert_admitted_watch(
            watch,
            md=AsyncMock(),
            settings=MagicMock(fred_api_key=None, finnhub_api_key=None),
            audit=AsyncMock(),
            stats=stats,
            price=100.05,
            quote=_quote(ask="100.05"),
            admission=admission,
        )

    updated = ENTRY_WATCHES.get(watch.id)
    assert updated is not None
    assert updated.status is EntryWatchStatus.CONVERTING
    assert updated.converted_opportunity_id is None
    assert updated.trigger_version == 1
    assert ENTRY_WATCHES.claim_admission(key) is True


def test_recovery_finishes_converting_when_opportunity_already_published() -> None:
    watch = _admitted_watch(trigger_version=1)
    key = admission_claim_key(watch.id, watch.trigger_version)
    assert ENTRY_WATCHES.claim_admission(key) is True
    converting = ENTRY_WATCHES.mark(
        watch.id, EntryWatchStatus.CONVERTING, reason="CONVERSION_CLAIM"
    )
    assert converting is not None
    opp_id = uuid4()
    ENTRY_WATCHES.update(converting.model_copy(update={"converted_opportunity_id": opp_id}))

    stats = {"still_waiting": 0, "converted": 0, "invalidated": 0}
    _defer_to_recovery_revalidation(ENTRY_WATCHES.get(watch.id) or converting, stats=stats)

    updated = ENTRY_WATCHES.get(watch.id)
    assert updated is not None
    assert updated.status is EntryWatchStatus.CONVERTED
    assert updated.converted_opportunity_id == opp_id
    assert stats["converted"] == 1
    assert ENTRY_WATCHES.claim_admission(key) is False
