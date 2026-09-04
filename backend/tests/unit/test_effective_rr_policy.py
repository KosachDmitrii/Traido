"""Effective R:R floors follow entry aggressiveness."""

from __future__ import annotations

from trading.effective_rr import required_admission_rr
from trading.entry_policy import get_entry_thresholds


def test_weak_aggressiveness_does_not_raise_rr_for_weak_setup(monkeypatch) -> None:
    monkeypatch.setattr("trading.entry_policy._cached", 100)
    th = get_entry_thresholds()
    assert th.min_effective_rr == 1.45
    assert th.weak_setup_min_rr == 1.45
    req = required_admission_rr(
        setup_quality=48,
        entry_quality=50,
        chase_score=20,
        structure_valid=True,
        warnings=["STRUCTURAL_DAMAGE"],
        min_rr_floor=th.min_effective_rr,
        weak_setup_rr_floor=th.weak_setup_min_rr,
    )
    assert req == 1.45


def test_strong_aggressiveness_keeps_weak_setup_penalty(monkeypatch) -> None:
    monkeypatch.setattr("trading.entry_policy._cached", 0)
    th = get_entry_thresholds()
    req = required_admission_rr(
        setup_quality=48,
        entry_quality=50,
        chase_score=20,
        structure_valid=True,
        warnings=[],
        min_rr_floor=th.min_effective_rr,
        weak_setup_rr_floor=th.weak_setup_min_rr,
    )
    assert req == 2.50
