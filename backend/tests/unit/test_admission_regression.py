"""Section 7 regression tests — admission + WAIT path gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from core.enums import (
    AdmissionDecision,
    EntryDecision,
    EntryWatchStatus,
    InstrumentThesis,
    SetupType,
    TargetReachabilityClass,
    Timeframe,
)
from core.schemas import (
    AdmissionSnapshot,
    Bar,
    EntryDecisionBundle,
    EntryQualityBreakdown,
    EntryTimingFacts,
    EntryWatch,
    Quote,
    SetupQualityBreakdown,
    TargetPlan,
)
from trading.entry_policy import set_entry_aggressiveness
from trading.entry_timing import PULLBACK_TOO_DEEP
from trading.entry_watches import ENTRY_WATCHES
from trading.market_context import build_market_context, sector_etf_for
from trading.structural_integrity import evaluate_structural_integrity
from trading.trade_admission import evaluate_trade_admission
from trading.wait_engine_metrics import compute_wait_engine_metrics
from trading.zone_arrival import ArrivalType, evaluate_zone_arrival


def _bar(symbol: str, ts: datetime, o: float, h: float, l: float, c: float, v: float) -> Bar:
    return Bar(
        symbol=symbol,
        timeframe=Timeframe.H1,
        ts=ts,
        open=Decimal(str(o)),
        high=Decimal(str(h)),
        low=Decimal(str(l)),
        close=Decimal(str(c)),
        volume=Decimal(str(v)),
        source="test",
    )


def _bundle(
    *,
    price: float,
    setup_q: int = 85,
    entry_q: int = 75,
    zone_low: float = 111.8,
    zone_high: float = 113.2,
    stop: float = 108.0,
    target: float = 125.0,
    atr: float = 2.0,
    facts: EntryTimingFacts | None = None,
) -> EntryDecisionBundle:
    timing = facts or EntryTimingFacts(
        current_price=price,
        atr=atr,
        distance_from_vwap_pct=-2.0,
        distance_from_fast_ema_pct=3.0,
        stop_distance_atr=(price - stop) / atr if atr else None,
        nearest_support=stop + 5.0,
    )
    return EntryDecisionBundle(
        thesis=InstrumentThesis.BULLISH,
        entry_decision=EntryDecision.BUY_NOW,
        entry_quality=entry_q,
        setup_quality=setup_q,
        setup_breakdown=SetupQualityBreakdown(
            trend_structure=setup_q,
            impulse_quality=setup_q,
            retracement_structure=setup_q,
            volume_participation=setup_q,
            support_structure=setup_q,
            market_alignment=setup_q,
            catalyst=setup_q,
            liquidity=setup_q,
        ),
        breakdown=EntryQualityBreakdown(
            price_location=entry_q,
            vwap_location=entry_q,
            atr_extension=entry_q,
            pullback_quality=entry_q,
            remaining_reward=entry_q,
            support_structure=entry_q,
            resistance_structure=entry_q,
            short_term_momentum=entry_q,
            volume_confirmation=entry_q,
            market_alignment=entry_q,
            signal_drift=entry_q,
        ),
        facts=timing,
        entry_zone_low=Decimal(str(zone_low)),
        entry_zone_high=Decimal(str(zone_high)),
        stop_price=Decimal(str(stop)),
        target=TargetPlan(
            price=Decimal(str(target)),
            model="2R",
            reachability=TargetReachabilityClass.REALISTIC,
        ),
    )


def _quote(bid: float, ask: float) -> Quote:
    return Quote(
        symbol="NEM",
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        ts=datetime.now(UTC),
        source="test",
    )


def _watch() -> EntryWatch:
    now = datetime.now(UTC)
    return EntryWatch(
        id=uuid4(),
        symbol="NEM",
        strategy_version="test",
        created_at=now - timedelta(hours=1),
        valid_until=now + timedelta(hours=2),
        thesis=InstrumentThesis.BULLISH,
        signal_price=Decimal(123),
        current_price_at_creation=Decimal(123),
        entry_zone_low=Decimal("111.8"),
        entry_zone_high=Decimal("113.2"),
        planned_entry=Decimal("112.5"),
        planned_stop=Decimal(108),
        planned_target=Decimal(125),
        entry_quality_at_creation=70,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality_at_creation=84,
        status=EntryWatchStatus.TRIGGERED,
        trigger_version=1,
        admission_snapshot=AdmissionSnapshot(
            price_at_creation=123.0,
            atr_at_creation=2.0,
            setup_type=SetupType.PULLBACK_CONTINUATION,
            entry_zone_low=111.8,
            entry_zone_high=113.2,
        ),
    )


def _healthy_arrival_bars() -> list[Bar]:
    _watch()
    base = datetime.now(UTC) - timedelta(hours=24)
    bars: list[Bar] = []
    price = 123.0
    for i in range(20):
        vol = 1400.0 - i * 35
        if i % 3 == 0:
            o, c = price, price - 0.15
            price = c
        else:
            o, c = price - 0.05, price + 0.08
            price = c
        bars.append(
            _bar("NEM", base + timedelta(hours=i), o, max(o, c) + 0.05, min(o, c) - 0.05, c, vol)
        )
    return bars


@pytest.fixture(autouse=True)
def _reset_policy() -> None:
    set_entry_aggressiveness(0, actor="test")


def test_invalid_stop_blocks() -> None:
    bundle = _bundle(price=112.0, stop=113.0)
    admission = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(111.9, 112.0),
        entry=112.0,
        stop=113.0,
        target=125.0,
    )
    assert admission.admitted is False
    assert "INVALID_STOP" in admission.vetoes or any(
        "INVALID_STOP" in r for r in admission.reason_codes
    )


def test_missing_target_blocks() -> None:
    bundle = _bundle(price=112.0).model_copy(update={"target": None})
    admission = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(111.9, 112.0),
        entry=112.0,
        stop=108.0,
        target=None,
    )
    assert admission.admitted is False


def test_structural_damage_no_trade() -> None:
    facts = EntryTimingFacts(
        current_price=112.0,
        atr=2.0,
        nearest_support=115.0,
    )
    structure = evaluate_structural_integrity(facts, chase_reasons=[PULLBACK_TOO_DEEP])
    assert structure.hard_damage is True

    bundle = _bundle(price=112.0, facts=facts)
    admission = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(111.9, 112.0),
        entry=112.0,
        stop=108.0,
        target=125.0,
    )
    assert admission.decision is AdmissionDecision.WAIT
    assert admission.admitted is False
    assert "STRUCTURAL_DAMAGE" in admission.vetoes or any(
        "STRUCTURAL_DAMAGE" in r for r in admission.reason_codes
    )


def test_crash_arrival_blocks_buy() -> None:
    watch = _watch()
    base = datetime.now(UTC) - timedelta(hours=8)
    bars = [
        _bar("NEM", base + timedelta(hours=i), 123 - i, 123.5 - i, 112, 113, 1500 + i * 200)
        for i in range(6)
    ]
    bars[-1] = _bar("NEM", base + timedelta(hours=5), 121, 121.2, 110, 113, 3000)
    arrival = evaluate_zone_arrival(watch, bars, atr=2.0, current_price=113.0)
    assert arrival.arrival_type != ArrivalType.HEALTHY_PULLBACK

    bundle = _bundle(price=113.0, entry_q=78, setup_q=84, zone_low=111.8, zone_high=113.2)
    admission = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(112.9, 113.0),
        entry=112.5,
        stop=108.0,
        target=125.0,
        zone_arrival=arrival,
    )
    assert admission.admitted is False
    assert admission.decision is not AdmissionDecision.BUY_ALLOWED


def test_healthy_arrival_may_buy_allowed() -> None:
    watch = _watch()
    bars = _healthy_arrival_bars()
    arrival = evaluate_zone_arrival(watch, bars, atr=2.0, current_price=112.5)
    assert arrival.score >= 60

    facts = EntryTimingFacts(
        current_price=112.5,
        atr=2.0,
        distance_from_vwap_pct=-2.0,
        distance_from_fast_ema_pct=3.0,
        stop_distance_atr=(112.5 - 108.0) / 2.0,
        nearest_support=108.0,
    )
    bundle = _bundle(
        price=112.5,
        setup_q=82,
        entry_q=76,
        zone_low=111.8,
        zone_high=113.2,
        stop=108.0,
        target=125.0,
        facts=facts,
    )
    admission = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        quote=_quote(112.4, 112.5),
        entry=112.5,
        stop=108.0,
        target=125.0,
        zone_arrival=arrival,
    )
    assert admission.decision is AdmissionDecision.BUY_ALLOWED
    assert admission.admitted is True


def test_breakout_skips_zone_arrival_gate() -> None:
    bundle = _bundle(price=150.0, zone_low=145.0, zone_high=155.0, entry_q=80, setup_q=82)
    admission = evaluate_trade_admission(
        bundle=bundle,
        setup_type=SetupType.BREAKOUT_CONTINUATION,
        quote=_quote(149.9, 150.0),
        entry=150.0,
        stop=145.0,
        target=165.0,
        zone_arrival=None,
    )
    assert "ZONE_ARRIVAL" not in " ".join(admission.reason_codes)


def test_duplicate_admission_claim() -> None:
    ENTRY_WATCHES.clear()
    key = "watch:test:1"
    assert ENTRY_WATCHES.claim_admission(key) is True
    assert ENTRY_WATCHES.claim_admission(key) is False
    ENTRY_WATCHES.clear()


def test_market_context_nem_gdx() -> None:
    ctx = build_market_context(symbol="NEM")
    assert sector_etf_for("NEM") == "GDX"
    assert ctx.sector_etf == "GDX"


def test_wait_engine_metrics_empty() -> None:
    metrics = compute_wait_engine_metrics()
    assert metrics.sample_size >= 0
