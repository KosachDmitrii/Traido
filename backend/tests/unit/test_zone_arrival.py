"""Zone arrival path dependency tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from core.enums import EntryWatchStatus, InstrumentThesis, SetupType, Timeframe
from core.schemas import AdmissionSnapshot, Bar, EntryWatch
from trading.zone_arrival import ArrivalType, detect_crash_velocity, evaluate_zone_arrival


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


def _watch() -> EntryWatch:
    now = datetime.now(UTC)
    return EntryWatch(
        id=uuid4(),
        symbol="NEM",
        strategy_version="test",
        created_at=now - timedelta(hours=2),
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
        status=EntryWatchStatus.TRIGGERED,
        admission_snapshot=AdmissionSnapshot(
            price_at_creation=123.0,
            atr_at_creation=2.0,
            setup_type=SetupType.PULLBACK_CONTINUATION,
            entry_zone_low=111.8,
            entry_zone_high=113.2,
        ),
    )


def test_healthy_pullback_scores_high() -> None:
    watch = _watch()
    base = datetime.now(UTC) - timedelta(hours=24)
    bars = []
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
    arrival = evaluate_zone_arrival(watch, bars, atr=2.0, current_price=price)
    assert arrival.arrival_type == ArrivalType.HEALTHY_PULLBACK
    assert arrival.score >= 60


def test_crash_velocity_detected() -> None:
    assert detect_crash_velocity(decline_atr=2.0, bars=2, volume_ratio=2.0) is True


def test_fast_sell_off_low_score() -> None:
    watch = _watch()
    base = datetime.now(UTC) - timedelta(hours=8)
    bars = [
        _bar("NEM", base + timedelta(hours=i), 123 - i, 123.5 - i, 112, 113, 1500 + i * 200)
        for i in range(6)
    ]
    bars[-1] = _bar("NEM", base + timedelta(hours=5), 121, 121.2, 110, 113, 3000)
    arrival = evaluate_zone_arrival(watch, bars, atr=2.0, current_price=113.0)
    assert arrival.score <= 40
    assert arrival.arrival_type != ArrivalType.HEALTHY_PULLBACK


def test_refreshed_wait_mark_does_not_zero_path() -> None:
    """Scanner refresh resets price_at_creation — path must still use signal_price."""
    watch = _watch()
    watch = watch.model_copy(
        update={
            "current_price_at_creation": Decimal("112.5"),
            "last_price": Decimal("112.5"),
            "admission_snapshot": watch.admission_snapshot.model_copy(
                update={"price_at_creation": 112.5}
            ),
        }
    )
    base = datetime.now(UTC) - timedelta(hours=24)
    bars = []
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
    arrival = evaluate_zone_arrival(watch, bars, atr=2.0, current_price=112.5)
    assert arrival.arrival_type != ArrivalType.UNKNOWN
    assert arrival.score >= 60


def test_stale_overnight_gap_in_window_does_not_block_orderly_pullback() -> None:
    """A gap days ago in the 25-bar window must not mark every in-zone card GAP_DOWN."""
    watch = _watch()
    base = datetime(2026, 8, 25, 14, 0, tzinfo=UTC)
    bars = []
    price = 123.0
    for i in range(25):
        ts = base + timedelta(hours=i)
        if i >= 18:
            o, c = price - 0.2, price - 0.35
            price = c
        else:
            o, c = price - 0.05, price + 0.02
            price = c
        bars.append(_bar("NEM", ts, o, max(o, c) + 0.1, min(o, c) - 0.1, c, 1200.0))
    fri = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)
    mon = datetime(2026, 8, 29, 13, 30, tzinfo=UTC)
    bars[8] = _bar("NEM", fri, 120.0, 120.2, 119.8, 120.0, 1000.0)
    bars[9] = _bar("NEM", mon, 116.0, 116.5, 115.5, 116.2, 2000.0)
    arrival = evaluate_zone_arrival(watch, bars, atr=2.0, current_price=112.5)
    assert arrival.arrival_type != ArrivalType.GAP_DOWN
