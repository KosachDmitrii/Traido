"""Impulse / pullback leg metrics and professional entry zone."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from core.enums import Timeframe
from core.schemas import Bar
from quant.engine import compute_features
from quant.impulse_pullback import compute_impulse_pullback
from trading.entry_policy import set_entry_aggressiveness
from trading.entry_timing import (
    PULLBACK_TOO_DEEP,
    PULLBACK_VOL_HEAVY,
    detect_chasing,
    evaluate_timing,
    zone_from_facts,
)


def _bars_uptrend_impulse_pullback() -> list[Bar]:
    """Synthetic uptrend: grind up, impulse, partial pullback."""
    t0 = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)
    bars: list[Bar] = []
    price = 100.0
    for i in range(25):
        if i < 10:
            o, c = price, price + 0.15
        elif i < 15:
            o, c = price, price + 0.55
        else:
            o, c = price, price - 0.20
        h = max(o, c) + 0.05
        l = min(o, c) - 0.05
        vol = 1_000_000 if i < 15 else 400_000
        bars.append(
            Bar(
                symbol="TEST",
                timeframe=Timeframe.H1,
                source="test",
                ts=t0 + timedelta(hours=i),
                open=Decimal(str(round(o, 4))),
                high=Decimal(str(round(h, 4))),
                low=Decimal(str(round(l, 4))),
                close=Decimal(str(round(c, 4))),
                volume=Decimal(str(vol)),
            )
        )
        price = c
    return bars


def test_impulse_pullback_detects_retracement() -> None:
    bars = _bars_uptrend_impulse_pullback()
    m = compute_impulse_pullback(bars, atr=1.0, anchor=107.0)
    assert m.impulse_high is not None and m.impulse_low is not None
    assert m.retracement_pct is not None and m.retracement_pct > 0
    assert m.impulse_grade in {"A", "B", "C"}
    assert m.pullback_vol_ratio is not None and m.pullback_vol_ratio < 1.0


def test_quant_engine_exports_leg_indicators() -> None:
    bars = _bars_uptrend_impulse_pullback()
    snap = compute_features("TEST", Timeframe.H1, bars)
    assert snap.indicators.get("impulse_high") is not None
    assert snap.indicators.get("retracement_pct") is not None


def test_professional_zone_wider_undercut_than_legacy() -> None:
    facts = evaluate_timing(
        compute_features("TEST", Timeframe.H1, _bars_uptrend_impulse_pullback()),
        signal_price=108.0,
        planned_entry=107.0,
        planned_stop=105.0,
        planned_target=112.0,
    )
    set_entry_aggressiveness(0, actor="test")
    low, high = zone_from_facts(facts)
    anchor = facts.anchor_price or facts.current_price
    atr = facts.atr or 1.0
    assert float(low) <= anchor - 0.45 * atr
    assert float(high) <= anchor + 0.25 * atr + 0.01


def test_deep_pullback_is_hard_veto() -> None:
    from core.enums import EntryDecision, InstrumentThesis
    from trading.entry_quality import decide_entry

    bars = _bars_uptrend_impulse_pullback()
    snap = compute_features("TEST", Timeframe.H1, bars)
    facts = evaluate_timing(
        snap,
        signal_price=110.0,
        planned_entry=107.0,
        planned_stop=105.0,
        planned_target=112.0,
    )
    facts = facts.model_copy(update={"retracement_pct": 0.85, "impulse_grade": "B"})
    chase = detect_chasing(facts)
    assert PULLBACK_TOO_DEEP in chase
    bundle = decide_entry(InstrumentThesis.BULLISH, facts, technical_score=80)
    assert bundle.entry_decision is EntryDecision.NO_TRADE


def test_heavy_pullback_volume_flags_chase() -> None:
    bars = _bars_uptrend_impulse_pullback()
    snap = compute_features("TEST", Timeframe.H1, bars)
    facts = evaluate_timing(
        snap,
        signal_price=108.0,
        planned_entry=107.0,
        planned_stop=105.0,
        planned_target=112.0,
    )
    facts = facts.model_copy(update={"pullback_vol_ratio": 1.4})
    assert PULLBACK_VOL_HEAVY in detect_chasing(facts)
