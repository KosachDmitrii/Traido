"""Company-level Nasdaq sectors; no industry guessing or price data.

One public screener read serves the whole universe. Only explicit sector labels
are accepted, tied to the exact symbol and the directory observation time.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from core.vendor_http import describe_http_error, get_with_retry

URL = "https://api.nasdaq.com/api/screener/stocks"
STORE_KEY = "__NASDAQ_SECTOR_DIRECTORY__"
VERSION = "nasdaq_sector_directory@1"
TTL = timedelta(days=7)
FAILURE_TTL = timedelta(minutes=2)
logger = logging.getLogger(__name__)
SECTORS = {
    "technology": "technology",
    "telecommunications": "communication",
    "communication services": "communication",
    "consumer discretionary": "consumer_discretionary",
    "consumer staples": "consumer_staples",
    "finance": "financials",
    "financials": "financials",
    "health care": "healthcare",
    "healthcare": "healthcare",
    "energy": "energy",
    "industrials": "industrials",
    "basic materials": "materials",
    "utilities": "utilities",
    "real estate": "real_estate",
}


def map_sector(label: object) -> str | None:
    return SECTORS.get(" ".join(label.lower().split())) if isinstance(label, str) else None


def parse_rows(payload: object) -> dict[str, dict[str, str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise TypeError("NASDAQ_DIRECTORY_INVALID")
    data = payload["data"]
    rows = data.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("NASDAQ_DIRECTORY_EMPTY")
    total = data.get("totalrecords")
    if total is not None and int(total) != len(rows):
        raise ValueError("NASDAQ_DIRECTORY_TRUNCATED")
    result: dict[str, dict[str, str]] = {}
    conflicts: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("NASDAQ_DIRECTORY_INVALID_ROW")
        symbol, label, industry = row.get("symbol"), row.get("sector"), row.get("industry")
        if not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper():
            continue
        if not isinstance(label, str) or map_sector(label) is None or not isinstance(industry, str):
            continue
        entry = {"symbol": symbol, "source_sector": label, "industry": industry}
        if symbol in result and result[symbol] != entry:
            conflicts.add(symbol)
        result[symbol] = entry
    for symbol in conflicts:
        result.pop(symbol, None)
    if not result:
        raise ValueError("NASDAQ_DIRECTORY_NO_SECTORS")
    return result


@dataclass(frozen=True)
class NasdaqClassification:
    symbol: str
    sector: str
    source_sector: str
    industry: str
    fetched_at: datetime


class NasdaqSectorDirectory:
    def __init__(
        self, *, transport: httpx.AsyncBaseTransport | None = None, persistent: bool = False
    ) -> None:
        self._transport = transport
        self._persistent = persistent
        self._rows: dict[str, dict[str, str]] = {}
        self._fetched_at: datetime | None = None
        self._retry_at: datetime | None = None
        self._lock = asyncio.Lock()

    def _fresh(self, now: datetime) -> bool:
        return self._fetched_at is not None and timedelta(0) <= now - self._fetched_at <= TTL

    async def lookup(
        self, symbol: str, *, now: datetime | None = None
    ) -> NasdaqClassification | None:
        now = (now or datetime.now(UTC)) if self._transport is not None else datetime.now(UTC)
        if not self._fresh(now):
            async with self._lock:
                if not self._fresh(now):
                    if self._retry_at and now < self._retry_at:
                        return None
                    try:
                        if self._persistent:
                            from market_data.providers import sector_store

                            saved = await asyncio.to_thread(sector_store.read, STORE_KEY)
                            self._restore(saved, now)
                        if not self._fresh(now):
                            async with httpx.AsyncClient(
                                timeout=12, transport=self._transport
                            ) as client:
                                response = await asyncio.wait_for(
                                    get_with_retry(
                                        client,
                                        URL,
                                        params={
                                            "tableonly": "true",
                                            "limit": 10000,
                                            "download": "true",
                                        },
                                        headers={
                                            "User-Agent": "Mozilla/5.0",
                                            "Accept": "application/json",
                                            "Origin": "https://www.nasdaq.com",
                                        },
                                        attempts=2,
                                    ),
                                    timeout=30,
                                )
                                rows = parse_rows(response.json())
                            fetched_at = now if self._transport is not None else datetime.now(UTC)
                            if self._persistent:
                                await asyncio.to_thread(
                                    sector_store.write,
                                    STORE_KEY,
                                    {
                                        "version": VERSION,
                                        "fetched_at": fetched_at.isoformat(),
                                        "rows": rows,
                                    },
                                )
                            self._rows, self._fetched_at = rows, fetched_at
                            logger.info("Nasdaq sector directory refreshed: symbols=%s", len(rows))
                    except (httpx.HTTPError, ValueError, TypeError, TimeoutError) as exc:
                        self._retry_at = (
                            now if self._transport is not None else datetime.now(UTC)
                        ) + FAILURE_TTL
                        logger.warning(
                            "Nasdaq sector directory unavailable: %s", describe_http_error(exc)
                        )
                        return None
        row = self._rows.get(symbol)
        if row is None or self._fetched_at is None:
            return None
        sector = map_sector(row["source_sector"])
        if sector is None:
            return None
        return NasdaqClassification(
            symbol, sector, row["source_sector"], row["industry"], self._fetched_at
        )

    def _restore(self, saved: Any, now: datetime) -> None:
        if not isinstance(saved, dict) or saved.get("version") != VERSION:
            return
        try:
            timestamp = datetime.fromisoformat(saved["fetched_at"])
            if timestamp.tzinfo is None or not timedelta(0) <= now - timestamp <= TTL:
                return
            rows = saved["rows"]
            if not isinstance(rows, dict) or any(
                not isinstance(row, dict)
                or row.get("symbol") != symbol
                or map_sector(row.get("source_sector")) is None
                or not isinstance(row.get("industry"), str)
                for symbol, row in rows.items()
            ):
                return
            if rows:
                self._rows, self._fetched_at = rows, timestamp
        except (KeyError, TypeError, ValueError):
            return
