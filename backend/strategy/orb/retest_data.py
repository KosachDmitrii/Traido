"""Durable history, small incremental requests and independent symbol recovery."""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from core.enums import Timeframe
from core.schemas import Bar
from market_data.bar_store import load_bars, save_bars
from strategy.orb import OrbPlan

logger = logging.getLogger(__name__)
_cache: dict[tuple[str, str, str, datetime], tuple[list[Bar], datetime]] = {}
_failures: dict[tuple[str, str, str], tuple[datetime, Exception]] = {}
_cursor = 0
_probe_after: dict[str, datetime] = {}
_last_attempt: dict[tuple[str, str, str], datetime] = {}


def _boundary(now: datetime) -> datetime:
    end = now.astimezone(UTC).replace(second=0, microsecond=0)
    return end.replace(minute=end.minute - end.minute % 5)


def first_gap(plan: OrbPlan, rows: list[Bar], end: datetime) -> datetime:
    expected = plan.range_end
    for bar in sorted(rows, key=lambda b: b.ts):
        if bar.ts != expected:
            break
        expected += timedelta(minutes=5)
    return min(expected, end)


def _remember(plan: OrbPlan, end: datetime, rows: list[Bar], now: datetime) -> None:
    complete = first_gap(plan, rows, end) == end
    expires = end + timedelta(minutes=5) if complete else now + timedelta(seconds=5)
    if len(_cache) > 1024:
        _cache.clear()
    _cache[(plan.symbol, plan.session, plan.source, end)] = (rows, expires)


async def prime_bars(market_data: Any, plans: list[OrbPlan], *, now: datetime) -> None:
    """Six groups of five, two concurrently, 30-minute windows; failures shrink to probes."""
    global _cursor
    started = time.monotonic()
    batch = getattr(market_data, "get_bars_batch", None)
    if not callable(batch) or not plans:
        return
    end = _boundary(now)
    jobs: dict[tuple[datetime, datetime, str, str], list[OrbPlan]] = defaultdict(list)
    for plan in plans:
        key = (plan.symbol, plan.session, plan.source)
        feed = getattr(market_data, "_feed", None)
        if feed is not None and plan.source != f"alpaca:{feed}":
            _failures[key] = (now + timedelta(seconds=30), ValueError("ORB_DATA_FEED_MISMATCH"))
            continue
        if now < _cache.get((*key, end), ([], now))[1]:
            continue
        failure = _failures.get(key)
        if failure and now < failure[0]:
            continue
        rows = await asyncio.to_thread(load_bars, plan.source, plan.symbol, plan.range_end, end)
        _remember(plan, end, rows, now)
        gap = first_gap(plan, rows, end)
        from market_data.iex_stream import current

        if gap == end and current(plan.symbol, plan.source, now):
            _failures.pop(key, None)
            continue
        start = gap if gap < end else max(plan.range_end, end - timedelta(minutes=10))
        if start >= end:
            continue
        stop = min(end, start + timedelta(minutes=30))
        jobs[(start, stop, plan.source, plan.symbol if failure else "")].append(plan)
    groups = [
        (start, stop, members[i : i + 5])
        for (start, stop, _source, _probe), members in jobs.items()
        for i in range(0, len(members), 5)
    ]
    if not groups:
        return
    groups.sort(
        key=lambda group: max(
            _last_attempt.get((p.symbol, p.session, p.source), datetime.min.replace(tzinfo=UTC))
            for p in group[2]
        )
    )
    selected = groups[:6]
    _cursor += len(selected)
    semaphore = asyncio.Semaphore(2)

    async def fetch(start: datetime, stop: datetime, members: list[OrbPlan]) -> None:
        async with semaphore:
            for plan in members:
                _last_attempt[(plan.symbol, plan.session, plan.source)] = now
            try:
                response = await asyncio.wait_for(
                    batch(
                        [p.symbol for p in members],
                        start,
                        stop - timedelta(microseconds=1),
                        Timeframe.M5,
                    ),
                    timeout=12,
                )
                for plan in members:
                    rows = response.get(plan.symbol, [])
                    if any(not start <= b.ts < stop for b in rows):
                        raise ValueError("ORB_RETEST_DATA_INVALID")
                    await asyncio.to_thread(save_bars, plan.source, plan.symbol, rows)
                    expected = [
                        start + timedelta(minutes=5 * i)
                        for i in range(int((stop - start).total_seconds() / 300))
                    ]
                    if sorted(b.ts for b in rows) != expected:
                        _failures[(plan.symbol, plan.session, plan.source)] = (
                            now + timedelta(seconds=5),
                            ValueError("ORB_RETEST_HISTORY_GAP"),
                        )
                        _cache.pop((plan.symbol, plan.session, plan.source, end), None)
                        continue
                    _failures.pop((plan.symbol, plan.session, plan.source), None)
                    merged = await asyncio.to_thread(
                        load_bars, plan.source, plan.symbol, plan.range_end, end
                    )
                    _remember(plan, end, merged, now)
                logger.info(
                    "ORB bars loaded: symbols=%s start=%s end=%s rows=%s",
                    ",".join(p.symbol for p in members),
                    start.isoformat(),
                    stop.isoformat(),
                    sum(len(v) for v in response.values()),
                )
            except Exception as exc:  # noqa: BLE001 — isolate one group, never fabricate bars
                from strategy.orb.data_access import data_error_reason

                for plan in members:
                    _failures[(plan.symbol, plan.session, plan.source)] = (
                        now + timedelta(seconds=time.monotonic() - started + 30),
                        exc,
                    )
                    _cache.pop((plan.symbol, plan.session, plan.source, end), None)
                logger.warning(
                    "ORB bars unavailable: symbols=%s start=%s end=%s reason=%s",
                    ",".join(p.symbol for p in members),
                    start.isoformat(),
                    stop.isoformat(),
                    data_error_reason(exc),
                )

    await asyncio.gather(*(fetch(*group) for group in selected))
    # A single recent-window probe distinguishes history/batch failures from a
    # failure of even the smallest read. Keep its evidence without hiding gaps.
    source = plans[0].source
    quoter = getattr(market_data, "get_bars", None)
    if (
        callable(quoter)
        and now >= _probe_after.get(source, now)
        and all(
            (p.symbol, p.session, p.source) in _failures for _, _, group in selected for p in group
        )
    ):
        _probe_after[source] = now + timedelta(minutes=5)
        plan = selected[0][2][0]
        start = max(plan.range_end, end - timedelta(minutes=15))
        try:
            recent = await asyncio.wait_for(
                quoter(plan.symbol, Timeframe.M5, start, end - timedelta(microseconds=1)), timeout=8
            )
            if any(not start <= b.ts < end for b in recent):
                raise ValueError("ORB_RETEST_DATA_INVALID")
            await asyncio.to_thread(save_bars, source, plan.symbol, recent)
            logger.info(
                "ORB single-symbol probe: symbol=%s minutes=15 rows=%s", plan.symbol, len(recent)
            )
        except Exception as exc:  # noqa: BLE001 — diagnostic read has no trading authority
            from strategy.orb.data_access import data_error_reason

            logger.warning(
                "ORB single-symbol probe failed: symbol=%s minutes=15 reason=%s",
                plan.symbol,
                data_error_reason(exc),
            )


async def read_bars(
    market_data: Any, plan: OrbPlan, *, now: datetime, cached: bool = False
) -> list[Bar]:
    end = _boundary(now)
    feed = getattr(market_data, "_feed", None)
    if feed is not None and plan.source != f"alpaca:{feed}":
        raise ValueError("ORB_DATA_FEED_MISMATCH")
    rows = await asyncio.to_thread(load_bars, plan.source, plan.symbol, plan.range_end, end)
    from market_data.iex_stream import current

    if first_gap(plan, rows, end) == end and current(plan.symbol, plan.source, now):
        return rows
    if cached and callable(getattr(market_data, "get_bars_batch", None)):
        failure = _failures.get((plan.symbol, plan.session, plan.source))
        if failure:
            raise failure[1]
        return rows
    if not callable(getattr(market_data, "get_bars_batch", None)):
        # Capability fallback for single-symbol providers. The same 30-minute
        # bound applies, and the entire returned series is revalidated.
        result: list[Bar] = []
        start = plan.range_end
        while start < end:
            stop = min(end, start + timedelta(minutes=30))
            chunk = await asyncio.wait_for(
                market_data.get_bars(
                    plan.symbol, Timeframe.M5, start, stop - timedelta(microseconds=1)
                ),
                timeout=12,
            )
            if any(not start <= b.ts < stop for b in chunk):
                raise ValueError("ORB_RETEST_DATA_INVALID")
            result.extend(chunk)
            start = stop
        await asyncio.to_thread(save_bars, plan.source, plan.symbol, result)
        return result
    if first_gap(plan, rows, end) < end and end - plan.range_end > timedelta(minutes=30):
        raise ValueError("ORB_RETEST_HISTORY_GAP")
    start = (
        max(plan.range_end, end - timedelta(minutes=10))
        if rows and first_gap(plan, rows, end) == end
        else plan.range_end
    )
    fresh = (
        await asyncio.wait_for(
            market_data.get_bars(plan.symbol, Timeframe.M5, start, end - timedelta(microseconds=1)),
            timeout=12,
        )
        if end > start
        else []
    )
    if any(not start <= b.ts < end for b in fresh):
        raise ValueError("ORB_RETEST_DATA_INVALID")
    await asyncio.to_thread(save_bars, plan.source, plan.symbol, fresh)
    # Missing rows in a fresh response cannot be replaced by stale cached rows.
    return [b for b in rows if b.ts < start] + fresh
