"""Nearest levels must survive truncation before stop/target planning."""

from decimal import Decimal

import pytest

from quant.support_resistance import support_resistance


def _series():
    # Each five-point island has one unique high and low. Levels straddle 35.
    highs, lows = [], []
    for level in (10, 20, 30, 40, 50, 60):
        highs.extend([level + 1, level + 2, level + 3, level + 2, level + 1])
        lows.extend([level + 1, level, level - 1, level, level + 1])
    return highs, lows


def test_closest_supports_and_resistances_are_selected_on_correct_side():
    highs, lows = _series()
    support, resistance = support_resistance(highs, lows, reference_price=35, keep=2)
    assert support == [Decimal(19), Decimal(29)]
    assert resistance == [Decimal(43), Decimal(53)]
    # The old path kept the highest supports (49/59), then entry discarded both.
    old_support, _ = support_resistance(highs, lows, keep=2)
    assert all(level > 35 for level in old_support)


def test_levels_at_reference_are_not_claimed_as_distance_to_invalidation():
    highs, lows = _series()
    support, resistance = support_resistance(highs, lows, reference_price=29)
    assert all(p < 29 for p in support)
    assert all(p > 29 for p in resistance)


@pytest.mark.parametrize("price", [0, -1, float("nan"), float("inf")])
def test_invalid_reference_does_not_select_plausible_levels(price):
    with pytest.raises(ValueError, match="INVALID_LEVEL_REFERENCE_PRICE"):
        support_resistance(*_series(), reference_price=price)
