"""Bounded background metadata preparation, independent of entry observation."""

import asyncio
import logging
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

from core.config import get_settings
from core.universe import UNKNOWN_SECTOR, default_universe
from market_data.providers.sector import get_sector_resolver
from universe.exposure_policy import geared_exposure

logger = logging.getLogger(__name__)
MAX_PENDING = 5000
_pending: OrderedDict[str, None] = OrderedDict()
_task: asyncio.Task[None] | None = None


def offer(data: dict[str, Any]) -> None:
    """Queue metadata early; never delay a scanner/entry pass or change a plan."""
    if not get_sector_resolver(get_settings().finnhub_api_key).configured:
        return
    plans = data.get("plans") or {}
    pool = data.get("discovery_pool") or {}
    symbols = dict.fromkeys([*plans, *pool])
    universe = default_universe()
    resolver = get_sector_resolver(get_settings().finnhub_api_key)
    for symbol in symbols:
        raw = plans.get(symbol) or pool.get(symbol) or {}
        evidence = raw.get("evidence") or {}
        instrument = evidence.get("instrument") or raw.get("instrument") or {}
        if (
            instrument.get("asset_class") == "etf"
            or geared_exposure(instrument.get("classification_evidence") or {})
            or universe.sector_of(symbol) != UNKNOWN_SECTOR
        ):
            _pending.pop(symbol, None)
            continue
        if resolver._cached(symbol, datetime.now(UTC)) is not None:
            _pending.pop(symbol, None)
            continue
        if len(_pending) >= MAX_PENDING and symbol not in _pending:
            break
        _pending[symbol] = None
        if evidence.get("retest"):
            _pending.move_to_end(symbol, last=False)


async def _run() -> None:
    from market_data.providers import sector_store

    # Hydrate only validated observations. A deployment does not restart a
    # thousand vendor reads, and expired/old-version evidence cannot pass.
    try:
        resolver = get_sector_resolver(get_settings().finnhub_api_key)
        for symbol, raw in (await asyncio.to_thread(sector_store.observations)).items():
            entry = resolver._restore(symbol, raw)
            if entry is not None:
                resolver._cache.setdefault(symbol, entry)
    except Exception:
        logger.exception("Sector preflight hydration failed; ordinary resolver will retry")
    while True:
        if not _pending:
            await asyncio.sleep(1)
            continue
        symbol, _ = _pending.popitem(last=False)
        try:
            resolver = get_sector_resolver(get_settings().finnhub_api_key)
            if resolver._cached(symbol, datetime.now(UTC)) is not None:
                continue
            info = await resolver.resolve(symbol)
            if not info.available:
                from core.activity import BOARD

                BOARD.log("scanner", f"Sector metadata pending: {symbol}: {info.note}")
        except asyncio.CancelledError:
            raise
        except Exception:
            # A storage/vendor problem does not stop price observation or exits.
            logger.exception("Sector preflight failed for %s; retry on next scan", symbol)
        # Bound background work even for cache hits; the resolver also paces
        # each actual HTTP attempt. Price/benchmark admission remains current.
        await asyncio.sleep(3)


def start() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_run())


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
    _pending.clear()


def pending_count() -> int:
    return len(_pending)
