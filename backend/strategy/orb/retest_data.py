"""Bounded five-minute bar cache for observation; approval always reads afresh."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from core.enums import Timeframe
from core.schemas import Bar
from strategy.orb import OrbPlan

_cache: dict[tuple[str, str, str, datetime], tuple[list[Bar], datetime]] = {}
_failures: dict[tuple[str, str], tuple[datetime, Exception]] = {}


def _boundary(now: datetime) -> datetime:
    end = now.astimezone(UTC).replace(second=0, microsecond=0)
    return end.replace(minute=end.minute - end.minute % 5)


def _remember(plan: OrbPlan, end: datetime, rows: list[Bar], now: datetime) -> None:
    expected = int((end - plan.range_end).total_seconds() // 300)
    complete = len(rows) == expected and all(
        b.ts == plan.range_end + timedelta(minutes=5 * i)
        for i, b in enumerate(sorted(rows, key=lambda b: b.ts))
    )
    expires = end + timedelta(minutes=5) if complete else now + timedelta(seconds=5)
    if len(_cache) > 1024:
        _cache.clear()
    _cache[(plan.symbol, plan.session, plan.source, end)] = (rows, expires)


async def prime_bars(market_data: Any, plans: list[OrbPlan], *, now: datetime) -> None:
    """One paginated batch per completed window; bounded retry during an outage."""
    batch = getattr(market_data, "get_bars_batch", None)
    if not callable(batch) or not plans:
        return
    end = _boundary(now)
    key = (plans[0].session, plans[0].source)
    prior = _failures.get(key)
    if prior and now < prior[0]:
        raise prior[1]
    missing = [
        p for p in plans if now >= _cache.get((p.symbol, p.session, p.source, end), (None, now))[1]
    ]
    if not missing:
        return
    try:
        rows = (
            await asyncio.wait_for(
                batch(
                    [p.symbol for p in missing],
                    missing[0].range_end.astimezone(UTC),
                    end - timedelta(microseconds=1),
                    Timeframe.M5,
                ),
                timeout=15,
            )
            if end > missing[0].range_end
            else {}
        )
    except Exception as exc:
        _failures.clear()
        _failures[key] = (datetime.now(UTC) + timedelta(seconds=30), exc)
        raise
    _failures.pop(key, None)
    for plan in missing:
        _remember(plan, end, rows.get(plan.symbol, []), now)


async def read_bars(
    market_data: Any, plan: OrbPlan, *, now: datetime, cached: bool = False
) -> list[Bar]:
    end = _boundary(now)
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
        _remember(plan, end, rows, now)
    return rows
