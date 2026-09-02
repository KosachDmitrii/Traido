"""Final pretrade validation tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from core.enums import InstrumentThesis, SetupType, Timeframe, TradeAction
from core.schemas import AdmissionSnapshot, FeatureSnapshot, Quote, TradeCandidate
from trading.final_pretrade import PretradeRejection, final_pretrade_validation


def _snap(symbol: str = "NEM", close: float = 112.0, atr: float = 2.0) -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol=symbol,
        timeframe=Timeframe.H1,
        computed_at=datetime.now(UTC),
        indicators={"close": close, "atr_14": atr, "sma_20": close - 1, "vwap": close - 0.5},
        candlestick_patterns={},
        chart_patterns={},
        support=[Decimal(str(close - 4))],
        resistance=[Decimal(str(close + 8))],
    )


def _candidate(*, entry: float = 112.0, stop: float = 108.0, target: float = 120.0) -> TradeCandidate:
    return TradeCandidate(
        symbol="NEM",
        action=TradeAction.BUY,
        confidence=0.85,
        entry=Decimal(str(entry)),
        stop=Decimal(str(stop)),
        target=Decimal(str(target)),
        risk_reward=2.0,
        reasons=["test"],
        strategy_version="test@1",
        thesis=InstrumentThesis.BULLISH,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality=85,
        entry_quality=78,
        entry_zone_low=Decimal("111.8"),
        entry_zone_high=Decimal("113.2"),
        admission_version="admission@1.1.0",
        admission_snapshot=AdmissionSnapshot(
            price_at_creation=112.0,
            atr_at_creation=2.0,
            setup_type=SetupType.PULLBACK_CONTINUATION,
            entry_zone_low=111.8,
            entry_zone_high=113.2,
            setup_quality_at_creation=85,
            entry_quality_at_creation=78,
            stop_at_creation=108.0,
            target_at_creation=120.0,
            effective_rr_at_creation=2.0,
        ).model_dump(mode="json"),
    )


def _quote(bid: float, ask: float, *, age_sec: float = 0.0) -> Quote:
    return Quote(
        symbol="NEM",
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        ts=datetime.now(UTC) - timedelta(seconds=age_sec),
        source="test",
    )


def _run(cand: TradeCandidate, quote: Quote, **kwargs):
    now = datetime.now(UTC)
    return final_pretrade_validation(
        cand,
        quote=quote,
        bars_count=60,
        last_bar_ts=now - timedelta(minutes=30),
        now=now,
        exec_snap=_snap(close=float(quote.ask)),
        **kwargs,
    )


def test_price_moved_after_buy_allowed_blocks() -> None:
    cand = _candidate()
    with pytest.raises(PretradeRejection) as exc:
        _run(cand, _quote(113.95, 114.0))
    assert exc.value.code in {"BUY_REJECTED_PRICE_MOVED", "PRICE_OUTSIDE_ENTRY_POLICY"}


def test_rr_dropped_blocks() -> None:
    snap = AdmissionSnapshot(
        price_at_creation=1168.47,
        atr_at_creation=8.0,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        entry_zone_low=1160.0,
        entry_zone_high=1170.0,
        setup_quality_at_creation=89,
        entry_quality_at_creation=81,
        stop_at_creation=1153.26,
        target_at_creation=1193.77,
    )
    cand = TradeCandidate(
        symbol="LLY",
        action=TradeAction.BUY,
        confidence=0.9,
        entry=Decimal("1168.47"),
        stop=Decimal("1153.26"),
        target=Decimal("1193.77"),
        risk_reward=1.66,
        reasons=["test"],
        strategy_version="test@1",
        thesis=InstrumentThesis.BULLISH,
        setup_type=SetupType.PULLBACK_CONTINUATION,
        setup_quality=89,
        entry_quality=81,
        entry_zone_low=Decimal("1160.0"),
        entry_zone_high=Decimal("1170.0"),
        admission_version="admission@1.1.0",
        admission_snapshot=snap.model_dump(mode="json"),
    )
    with pytest.raises(PretradeRejection) as exc:
        now = datetime.now(UTC)
        quote = Quote(
            symbol="LLY",
            bid=Decimal("1168.40"),
            ask=Decimal("1168.47"),
            ts=now,
            source="test",
        )
        final_pretrade_validation(
            cand,
            quote=quote,
            bars_count=60,
            last_bar_ts=now - timedelta(minutes=30),
            now=now,
            exec_snap=_snap("LLY", 1168.47, 8.0),
        )
    assert exc.value.code == "BUY_REJECTED_RR_DROPPED"


def test_day_old_quote_is_data_blocked() -> None:
    cand = _candidate()
    with pytest.raises(PretradeRejection) as exc:
        _run(cand, _quote(111.95, 112.0, age_sec=86400))
    assert exc.value.code == "BUY_REJECTED_STALE_DATA"
    assert "STALE_DATA" in exc.value.detail
