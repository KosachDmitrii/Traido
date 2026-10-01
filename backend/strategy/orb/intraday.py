"""Paper-only rolling discovery, independent of the immutable opening selection.

Each completed M5 range uses the same clock window in 14 prior sessions for
relative volume. It seeds a NEW breakout/retest, never an immediate BUY.
"""

import asyncio
import logging
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


async def refresh(ctx: Any, universe: Any, *, now: datetime | None = None) -> dict[str, Any] | None:
    now = now or datetime.now(UTC)
    if get_settings().broker_env is not BrokerEnvironment.PAPER or not us_equity_rth_open(now):
        return None
    local = now.astimezone(ET)
    end = local.replace(minute=local.minute - local.minute % 5, second=0, microsecond=0)
    start = end - timedelta(minutes=5)
    # Keep the opening strategy and leave time for breakout/retest/confirmation.
    if start.time() <= time(9, 30) or end.time() >= session_close(local.date()):
        return None
    day = str(local.date())
    async with _lock:
        saved = await asyncio.to_thread(read_session, day)
        if saved is None or saved.get("status") != "ready":
            return None
        previous = saved.get("intraday_discovery") or {}
        if previous.get("range_end") == end.isoformat():
            if previous.get("status") == "ready":
                return saved
            attempted = datetime.fromisoformat(previous["evaluated_at"])
            if now < attempted + timedelta(seconds=30):
                return saved
        diagnostics: dict[str, Any] = {
            "version": INTRADAY_VERSION,
            "range_start": start.isoformat(),
            "range_end": end.isoformat(),
            "evaluated_at": now.isoformat(),
            "status": "loading",
            "rejections": {},
        }
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
            symbols = [s for s in pool if s not in saved.get("plans", {})]
            diagnostics["checked"] = len(symbols)
            today = (
                await feed.get_bars_batch(
                    symbols, start, end - timedelta(microseconds=1), Timeframe.M5
                )
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
            for d in _previous_sessions(now, 14) if candidates else []:
                t = datetime.combine(d, start.time(), ET)
                response = await feed.get_bars_batch(
                    candidates,
                    t,
                    t + timedelta(minutes=5) - timedelta(microseconds=1),
                    Timeframe.M5,
                )
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
                rejections=rejected,
                rejection_counts=dict(Counter(r for rs in rejected.values() for r in rs)),
            )
            result = await asyncio.to_thread(
                merge_intraday_discovery, day, plans=plans, diagnostics=diagnostics
            )
            logger.info(
                "ORB intraday discovery: session=%s range=%s checked=%s added=%s reasons=%s",
                day,
                start.isoformat(),
                len(symbols),
                result["intraday_discovery"]["added"],
                diagnostics["rejection_counts"],
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
