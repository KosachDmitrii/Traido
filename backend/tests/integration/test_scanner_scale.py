"""The complete eligible pool participates in the opening-volume selection."""

import pytest

from strategy.orb.runtime import discover
from tests.unit.test_orb_policy import NOW
from tests.unit.test_orb_session import Context, Universe


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [1000, 2500])
async def test_large_universe_is_not_truncated_before_volume_ranking(size):
    symbols = [f"S{i:04}" for i in range(size)]
    ctx = Context(symbols)
    result = await discover(ctx, Universe(symbols), now=NOW)
    assert result["counts"]["eligible"] == size
    assert result["counts"]["qualified"] == size
    assert next(iter(result["plans"])) == symbols[-1]
    assert set(result["plans"]) == set(symbols)
    assert len(ctx.market_data.calls) == 15
    assert all(len(call[0]) == size for call in ctx.market_data.calls)
    assert len(result["outranked"]) + len(result["plans"]) == size
