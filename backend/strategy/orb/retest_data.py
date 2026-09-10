"""Bounded five-minute bar cache for observation; approval always reads afresh."""

from datetime import UTC, datetime, timedelta

from core.enums import Timeframe
from strategy.orb import OrbPlan

_cache = {}


async def read_bars(market_data, plan: OrbPlan, *, now: datetime, cached=False):
    end = now.astimezone(UTC).replace(second=0, microsecond=0)
    end = end.replace(minute=end.minute - end.minute % 5)
    key = (plan.symbol, plan.session, plan.source, end)
    if cached and key in _cache:
        rows, expires = _cache[key]
        if now < expires:
            return rows
    rows = (
        await market_data.get_bars(
            plan.symbol,
            Timeframe.M5,
            plan.range_end.astimezone(UTC),
            end - timedelta(microseconds=1),
        )
        if end > plan.range_end
        else []
    )
    if cached:
        if len(_cache) > 1024:
            _cache.clear()
        _cache[key] = (rows, now + timedelta(seconds=30))
    return rows
