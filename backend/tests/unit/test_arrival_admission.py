"""Per-level zone arrival gates."""

from __future__ import annotations

from trading.arrival_admission import evaluate_arrival_gate, evaluate_hard_arrival
from trading.entry_policy import thresholds_for
from trading.zone_arrival import ArrivalType, ZoneArrivalFacts


def _arrival(**kwargs: object) -> ZoneArrivalFacts:
    base = {
        "score": 55.0,
        "arrival_type": ArrivalType.NORMAL_PULLBACK,
        "arrival_speed_pct": None,
        "arrival_speed_atr": None,
        "atr_velocity": None,
        "bars_to_zone": 3,
        "red_bar_ratio": 0.4,
        "consecutive_red_bars": 1,
        "largest_red_bar_atr": 0.5,
        "sell_volume_ratio": 1.0,
        "volume_acceleration": 1.0,
        "gap_down_pct": None,
        "crash_velocity": False,
        "structural_damage": False,
        "reason_codes": [],
    }
    base.update(kwargs)
    return ZoneArrivalFacts(**base)  # type: ignore[arg-type]


def test_strong_blocks_sell_off() -> None:
    th = thresholds_for(0)
    gate = evaluate_arrival_gate(_arrival(arrival_type=ArrivalType.SELL_OFF, score=40.0), th)
    assert gate.blocked is True
    assert "ARRIVAL_TYPE_SELL_OFF" in gate.reason_codes


def test_weak_blocks_sell_off_until_validated() -> None:
    th = thresholds_for(100)
    gate = evaluate_arrival_gate(_arrival(arrival_type=ArrivalType.SELL_OFF, score=80.0), th)
    assert gate.blocked is True
    assert "ARRIVAL_TYPE_SELL_OFF" in gate.reason_codes


def test_weak_blocks_very_low_sell_off() -> None:
    th = thresholds_for(100)
    gate = evaluate_arrival_gate(_arrival(arrival_type=ArrivalType.SELL_OFF, score=5.0), th)
    assert gate.blocked is True


def test_crash_always_hard_veto() -> None:
    th = thresholds_for(100)
    gate = evaluate_arrival_gate(_arrival(arrival_type=ArrivalType.CRASH, score=90.0), th)
    assert gate.hard_veto is True
    assert gate.blocked is True


def test_structural_damage_stays_hard_at_weak() -> None:
    th = thresholds_for(100)
    gate = evaluate_arrival_gate(_arrival(structural_damage=True, score=50.0), th)
    assert gate.blocked is True
    assert gate.hard_veto is True
    assert "STRUCTURAL_DAMAGE" in gate.reason_codes


def test_weak_fast_pullback_at_28_passes_without_damage() -> None:
    th = thresholds_for(100)
    gate = evaluate_arrival_gate(
        _arrival(arrival_type=ArrivalType.FAST_PULLBACK, score=28.0, structural_damage=False),
        th,
    )
    assert gate.blocked is False


def test_weak_fast_pullback_below_floor_blocks() -> None:
    th = thresholds_for(100)
    gate = evaluate_arrival_gate(
        _arrival(arrival_type=ArrivalType.FAST_PULLBACK, score=25.0),
        th,
    )
    assert gate.blocked is True


def test_hard_arrival_ignores_quality_score() -> None:
    arrival = _arrival(arrival_type=ArrivalType.UNKNOWN, score=32.0)
    for level in (0, 50, 100):
        hard = evaluate_hard_arrival(
            arrival, structural_hard=thresholds_for(level).structural_arrival_hard
        )
        assert hard.blocked is False
        assert hard.hard_veto is False
        assert hard.reason_codes == []


def test_hard_arrival_crash_is_invariant() -> None:
    arrival = _arrival(arrival_type=ArrivalType.CRASH, score=90.0, crash_velocity=True)
    strong = evaluate_hard_arrival(arrival, structural_hard=True)
    weak = evaluate_hard_arrival(arrival, structural_hard=True)
    assert strong == weak
    assert strong.hard_veto is True
    assert "CRASH_VELOCITY" in strong.veto_codes
