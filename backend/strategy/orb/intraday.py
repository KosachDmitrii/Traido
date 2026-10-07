"""Paper-only rolling discovery, independent of the immutable opening selection.

Each completed M5 range uses the same clock window in 14 prior sessions for
relative volume. It seeds a NEW breakout/retest, never an immediate BUY.
"""

import asyncio
import logging
import time as monotonic_clock
from collections import Counter
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from typing import Any

from core.clock import ET
from core.config import get_settings
from core.enums import BrokerEnvironment, Timeframe
from core.schemas import Bar
from strategy.orb import INTRADAY_VERSION, _previous_sessions, _valid_bar, form_plan
from strategy.orb.data_access import data_error_reason
from strategy.orb.store import merge_intraday_discovery, read_session
from trading.session_hours import session_close, us_equity_rth_open
from universe.models import UniverseTier

logger = logging.getLogger(__name__)
_lock = asyncio.Lock()
_task: asyncio.Task[None] | None = None
_heartbeat: float | None = None


def pending_windows(saved: dict[str, Any], latest: datetime) -> list[datetime]:
    """Current window first, then one oldest gap; bounded to this RTH session."""
    completed = set(saved.get("intraday_completed_ranges", []))
    previous = saved.get("intraday_discovery") or {}
    if previous.get("status") == "ready":
        completed.add(previous["range_end"])
    end = datetime.combine(latest.date(), time(9, 40), ET)
    pending = []
    while end <= latest and end.time() < session_close(latest.date()):
        if end.isoformat() not in completed:
            pending.append(end)
        end += timedelta(minutes=5)
    if latest in pending:
        pending.remove(latest)
        pending.insert(0, latest)
    return pending


async def refresh(ctx: Any, universe: Any, *, now: datetime | None = None) -> dict[str, Any] | None:
    fixed_now = now
    if get_settings().broker_env is not BrokerEnvironment.PAPER or not us_equity_rth_open(
        now or datetime.now(UTC)
    ):
        return None
    async with _lock:
        # Re-read the clock after a competing refresh, rather than scanning a
        # stale "latest" window captured before waiting for the lock.
        now = fixed_now or datetime.now(UTC)
        if not us_equity_rth_open(now):
            return None
        local = now.astimezone(ET)
        end = local.replace(minute=local.minute - local.minute % 5, second=0, microsecond=0)
        start = end - timedelta(minutes=5)
        # Keep the opening strategy and leave time for breakout/retest/confirmation.
        if start.time() <= time(9, 30) or end.time() >= session_close(local.date()):
            return None
        day = str(local.date())
        saved = await asyncio.to_thread(read_session, day)
        if saved is None or saved.get("status") != "ready":
            return None
        previous = saved.get("intraday_discovery") or {}
        if previous.get("status") == "data_blocked":
            attempted = datetime.fromisoformat(previous["evaluated_at"])
            if now < attempted + timedelta(seconds=30):
                return saved
        pending = pending_windows(saved, end)
        if not pending:
            return saved
        end = pending[0]
        start = end - timedelta(minutes=5)
        diagnostics: dict[str, Any] = {
            "version": INTRADAY_VERSION,
            "range_start": start.isoformat(),
            "range_end": end.isoformat(),
            "evaluated_at": now.isoformat(),
            "status": "loading",
            "rejections": {},
            "pending_windows": len(pending),
            "window_lag_seconds": (local - end).total_seconds(),
        }
        started = monotonic_clock.monotonic()
        try:
            feed = ctx.market_data
            if getattr(feed, "_feed", None) != "sip" or not callable(
                getattr(feed, "get_bars_batch", None)
            ):
                raise ValueError("ORB_UNSUPPORTED_FEED")
            pool = saved.get("discovery_pool")
            if pool is None:
                # Upgrade an already running morning session, once; persist
                # source bars so a restart does not reload the daily universe.
                snapshot = await universe.get_scan_universe(tier=UniverseTier.BROAD, max_size=0)
                daily = await ctx.daily_bars(
                    list(snapshot.symbols),
                    now - timedelta(days=45),
                    datetime.combine(local.date(), time(9, 30), ET),
                )
                days = _previous_sessions(now, 15)
                pool = {}
                for instrument in snapshot.eligible:
                    rows = daily.get(instrument.key, [])
                    prior = {b.ts.astimezone(ET).date(): b for b in rows if _valid_bar(b)}
                    if any(d not in prior for d in days):
                        continue
                    bars = [prior[d] for d in days]
                    adv = sum((b.volume for b in bars[-14:]), Decimal(0)) / 14
                    atr = (
                        sum(
                            (
                                max(b.high - b.low, abs(b.high - a.close), abs(b.low - a.close))
                                for a, b in pairwise(bars)
                            ),
                            Decimal(0),
                        )
                        / 14
                    )
                    if adv < 1000000 or atr <= Decimal("0.50"):
                        continue
                    pool[instrument.key] = {
                        "daily": [b.model_dump(mode="json") for b in bars],
                        "instrument": {
                            "asset_class": instrument.asset_class.value,
                            "provider": instrument.provider,
                            "classification_evidence": dict(instrument.metadata),
                        },
                    }
                saved = await asyncio.to_thread(merge_intraday_discovery, day, pool=pool)
            from universe.exposure_policy import geared_exposure

            symbols = [
                s
                for s in pool
                if s not in saved.get("plans", {})
                and not geared_exposure(
                    (pool[s].get("instrument") or {}).get("classification_evidence") or {}
                )
            ]
            diagnostics["checked"] = len(symbols)

            async def load_window(wanted, window_start, window_end):
                # Bound each batch, not the whole universe. A market-wide
                # deadline would reject healthy large scans on account pacing.
                rows = {}
                for offset in range(0, len(wanted), 100):
                    rows.update(
                        await asyncio.wait_for(
                            feed.get_bars_batch(
                                wanted[offset : offset + 100],
                                window_start,
                                window_end,
                                Timeframe.M5,
                            ),
                            timeout=60,
                        )
                    )
                return rows

            today = (
                await load_window(symbols, start, end - timedelta(microseconds=1))
                if symbols
                else {}
            )
            # Fetch 14 historical windows only for a genuinely bullish, valid
            # current range. No daily-volume/time approximation or fake bars.
            candidates = []
            rejected: dict[str, list[str]] = {}
            for symbol in symbols:
                rows = today.get(symbol, [])
                if len(rows) != 1 or rows[0].ts != start or not _valid_bar(rows[0]):
                    rejected[symbol] = ["ORB_OPENING_RANGE_MISSING"]
                elif rows[0].close <= rows[0].open or rows[0].open <= 5:
                    rejected[symbol] = (
                        ["ORB_OPENING_NOT_BULLISH"]
                        if rows[0].close <= rows[0].open
                        else ["ORB_PRICE_BELOW_MINIMUM"]
                    )
                else:
                    candidates.append(symbol)
            windows: dict[str, list[Bar]] = {s: [] for s in candidates}
            semaphore = asyncio.Semaphore(2)

            async def historical_window(d):
                t = datetime.combine(d, start.time(), ET)
                async with semaphore:
                    return await load_window(
                        candidates,
                        t,
                        t + timedelta(minutes=5) - timedelta(microseconds=1),
                    )

            # Share the adapter's account quota, but overlap network latency.
            # Two bounded readers leave entry/watch requests their normal path.
            tasks = [
                asyncio.create_task(historical_window(d))
                for d in _previous_sessions(now, 14)
                if candidates
            ]
            try:
                responses = await asyncio.gather(*tasks)
            finally:
                # gather propagates the first failure without cancelling its
                # siblings. Drain them before releasing the discovery lock.
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            for response in responses:
                for symbol in candidates:
                    windows[symbol].extend(response.get(symbol, []))
            plans = {}
            for symbol in candidates:
                decision = form_plan(
                    symbol,
                    [Bar.model_validate(b) for b in pool[symbol]["daily"]],
                    windows[symbol] + today[symbol],
                    now=now,
                    feed="sip",
                    version=INTRADAY_VERSION,
                )
                if decision.plan is None:
                    rejected[symbol] = decision.reasons
                    continue
                plan = decision.plan.model_dump(mode="json")
                plan["evidence"]["instrument"] = pool[symbol]["instrument"]
                plans[symbol] = plan
            diagnostics.update(
                status="ready",
                elapsed_seconds=round(monotonic_clock.monotonic() - started, 3),
                rejections=rejected,
                rejection_counts=dict(Counter(r for rs in rejected.values() for r in rs)),
            )
            result = await asyncio.to_thread(
                merge_intraday_discovery, day, plans=plans, diagnostics=diagnostics
            )
            logger.info(
                "ORB intraday discovery: session=%s range=%s checked=%s added=%s reasons=%s "
                "elapsed_seconds=%s window_lag_seconds=%s pending_windows=%s",
                day,
                start.isoformat(),
                len(symbols),
                result["intraday_discovery"]["added"],
                diagnostics["rejection_counts"],
                diagnostics["elapsed_seconds"],
                diagnostics["window_lag_seconds"],
                diagnostics["pending_windows"] - 1,
            )
            from core.desk_bus import DESK_BUS

            DESK_BUS.bump_desk(kind="orb_intraday_discovery")
            return result
        except Exception as exc:  # noqa: BLE001 — discovery failure cannot disable exits/watch
            diagnostics.update(status="data_blocked", reason=data_error_reason(exc, feed="sip"))
            logger.warning(
                "ORB intraday discovery blocked: session=%s reason=%s", day, diagnostics["reason"]
            )
            return await asyncio.to_thread(merge_intraday_discovery, day, diagnostics=diagnostics)


async def discovery_loop() -> None:
    """An observation backlog cannot stop new M5 discovery. One reader only."""
    from agents.scanner.agent import load_watchlist, universe_service
    from market_data.sector_preflight import offer
    from risk.kill_switch import is_kill_switch_on
    from trading.scan_context import open_scan_context

    global _heartbeat
    while True:
        _heartbeat = monotonic_clock.monotonic()
        try:
            if (
                get_settings().broker_env is BrokerEnvironment.PAPER
                and us_equity_rth_open(datetime.now(UTC))
                and load_watchlist().get("enabled", True)
                and not is_kill_switch_on()
                and not _lock.locked()
            ):
                async with open_scan_context(get_settings()) as ctx:
                    data = await refresh(ctx, universe_service())
                    if data:
                        offer(data)
        except asyncio.CancelledError:
            raise
        except Exception:  # retry reads, never submit an order
            logger.exception("ORB discovery loop failed; retrying")
        _heartbeat = monotonic_clock.monotonic()
        await asyncio.sleep(30)


def start_discovery_loop() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(discovery_loop(), name="orb-intraday-discovery")


def stop_discovery_loop() -> None:
    global _task, _heartbeat
    if _task is not None:
        _task.cancel()
    _task = None
    _heartbeat = None


def discovery_health() -> tuple[bool, str]:
    if _task is not None and _task.done():
        return False, "intraday discovery task stopped"
    if _heartbeat is not None and monotonic_clock.monotonic() - _heartbeat > 300:
        return False, "intraday discovery has not progressed for over 300s"
    return True, "intraday discovery is progressing"
