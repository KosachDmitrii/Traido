"""Keep EntryWatch last marks fresh for the desk — independent of slow revalidate."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from core.desk_bus import DESK_BUS
from core.schemas import EntryWatch, Quote
from trading.entry_watches import ENTRY_WATCHES

logger = logging.getLogger(__name__)

# Desk poll is ~5s; marks older than this are refreshed on read as a backstop.
DESK_MARK_MAX_AGE_SEC = 8.0


async def fetch_mark_price(md: Any, symbol: str) -> tuple[float | None, Quote | None]:
    """Last trade when available, else quote mid. Never raises."""
    quote: Quote | None = None
    trade_ts: datetime | None = None
    try:
        if hasattr(md, "get_quote"):
            quote = await md.get_quote(symbol)
    except Exception:
        logger.debug("watch mark: quote failed for %s", symbol, exc_info=True)
        quote = None
    try:
        if hasattr(md, "get_latest_trade"):
            trade = await md.get_latest_trade(symbol)
            if trade is not None:
                last, trade_ts = trade
                if last > 0:
                    from market_data.quote_freshness import quote_with_trade_freshness

                    quote = quote_with_trade_freshness(quote, trade_ts=trade_ts)
                    return last, quote
        if hasattr(md, "get_last_price"):
            last = float(await md.get_last_price(symbol))
            if last > 0:
                from market_data.quote_freshness import quote_with_trade_freshness

                quote = quote_with_trade_freshness(quote, trade_ts=trade_ts)
                return last, quote
    except Exception:
        logger.debug("watch mark: last trade failed for %s", symbol, exc_info=True)
    if quote is not None:
        bid = float(quote.bid or 0)
        ask = float(quote.ask or 0)
        if bid > 0 and ask >= bid:
            return (bid + ask) / 2.0, quote
        px = float(quote.ask or quote.bid or 0)
        return (px or None), quote
    return None, quote


def stamp_watch_price(watch: EntryWatch, price: float) -> EntryWatch:
    """Persist the live mark in memory — every status, every pass."""
    prev = float(watch.last_price) if watch.last_price is not None else None
    out = ENTRY_WATCHES.touch_mark(watch.id, price) or watch
    if prev is None or abs(price - prev) / max(abs(prev), 1e-9) >= 0.0005:
        DESK_BUS.bump_desk(kind="entry_watch_price", symbol=out.symbol)
    return out


def _age_sec(watch: EntryWatch, *, now: datetime | None = None) -> float | None:
    if watch.last_observed_at is None:
        return None
    now = now or datetime.now(UTC)
    ts = watch.last_observed_at
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return max(0.0, (now - ts).total_seconds())


async def refresh_all_watch_marks(
    md: Any, watches: list[EntryWatch], *, concurrency: int = 6
) -> dict[str, tuple[float, Quote | None]]:
    """Parallel last-trade refresh for every actionable watch before heavy work."""
    if not watches:
        return {}

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(w: EntryWatch) -> tuple[str, float | None, Quote | None]:
        async with sem:
            price, quote = await fetch_mark_price(md, w.symbol)
            return w.symbol, price, quote

    results = await asyncio.gather(*[_one(w) for w in watches], return_exceptions=True)
    by_symbol: dict[str, tuple[float, Quote | None]] = {}
    for watch, result in zip(watches, results, strict=True):
        if isinstance(result, BaseException):
            logger.debug("watch mark gather failed for %s: %s", watch.symbol, result)
            continue
        _sym, price, quote = result
        if price is None or price <= 0:
            continue
        current = ENTRY_WATCHES.get(watch.id) or watch
        stamp_watch_price(current, price)
        by_symbol[watch.symbol] = (price, quote)
        try:
            from trading.shadow_outcomes import SHADOW_OUTCOMES

            SHADOW_OUTCOMES.update_price(watch.symbol, price)
        except Exception:  # noqa: BLE001, S110 — shadow marks are best-effort
            pass
    return by_symbol


async def refresh_stale_desk_marks(
    *,
    md: Any | None = None,
    max_age_sec: float = DESK_MARK_MAX_AGE_SEC,
    max_symbols: int = 8,
    timeout_sec: float = 2.5,
) -> int:
    """Desk-read backstop: refresh a few stale marks without blocking the rail.

    Full universe refresh belongs to the watch loop. This only patches the
    oldest stragglers, under a hard timeout, so a slow vendor cannot stall GET /desk.
    """
    watches = ENTRY_WATCHES.list_for_desk()
    now = datetime.now(UTC)
    ranked: list[tuple[float, EntryWatch]] = []
    for w in watches:
        age = _age_sec(w, now=now)
        if age is None:
            ranked.append((1e9, w))
        elif age >= max_age_sec:
            ranked.append((age, w))
    if not ranked:
        return 0
    ranked.sort(key=lambda row: row[0], reverse=True)
    stale = [w for _age, w in ranked[:max_symbols]]
    if md is None:
        from core.config import get_settings
        from market_data.factory import create_market_data_port

        md = create_market_data_port(get_settings())
    try:
        await asyncio.wait_for(refresh_all_watch_marks(md, stale), timeout=timeout_sec)
    except TimeoutError:
        logger.warning("desk mark refresh timed out after %.1fs (%d symbols)", timeout_sec, len(stale))
        return 0
    return len(stale)
