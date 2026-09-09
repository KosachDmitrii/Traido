from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.scanner.prefilter import MarketFilterPolicy
from agents.trader import universe
from agents.trader.types import TraderBundle
from universe.eligibility import EligibilityPolicy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "price,passed",
    [
        (5, True),
        (2500, True),
        (10000, True),
        (4.99, False),
        (10000.01, False),
        (None, False),
        (float("nan"), False),
        (float("inf"), False),
    ],
)
async def test_deep_price_band_matches_discovery(monkeypatch, price, passed):
    monkeypatch.setattr(
        universe, "check_bar_freshness", lambda *a, **kw: SimpleNamespace(passed=True)
    )
    monkeypatch.setattr(
        universe,
        "compute_features",
        lambda *a: SimpleNamespace(indicators={"close": price, "avg_dollar_volume": 0}),
    )
    md = SimpleNamespace(get_bars=AsyncMock(return_value=[object()] * universe.MIN_BARS))
    result = await universe.run_universe(TraderBundle(symbol="AZO"), md)
    # Deliberately stop at ADV; passing price alone must never imply trade eligibility.
    assert ("UNIVERSE_ADV" in result.reasons) is passed
    assert ("UNIVERSE_PRICE" in result.reasons) is not passed
    if not passed:
        assert "5–10000" in result.detail
    assert EligibilityPolicy().max_price == MarketFilterPolicy().max_price == universe.MAX_PRICE
    assert EligibilityPolicy().min_price == MarketFilterPolicy().min_price == universe.MIN_PRICE
