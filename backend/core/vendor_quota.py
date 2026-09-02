"""Process-wide vendor quota — pace by contract, not by guesswork.

Alpaca (Trading and Broker docs) return `X-RateLimit-Limit`,
`X-RateLimit-Remaining`, and `X-RateLimit-Reset` on responses. A 429 means
stop until that window (or `Retry-After`) clears. Hard-coded sleeps ignore what
the vendor already told us and either waste time or hammer a hot window.

This module is the single place that:

  1. Paces requests to a configured RPM floor (token bucket).
  2. Observes rate-limit headers on every response and slows before Remaining
     hits zero.
  3. On 429, cools down until Reset / Retry-After, with exponential+jitter only
     as a fallback when headers are missing (known to happen on some 429s).

One instance per API key. Callers share it so the scanner, entry-watch loop and
desk viability reads cannot each believe they own the whole quota.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from core.concurrency import RateLimiter

# Soft-throttle when Remaining drops below this fraction of Limit.
_SOFT_REMAINING_FRAC = 0.08
_SOFT_REMAINING_FLOOR = 4
# Never treat a missing Reset as "wait forever".
_MAX_HEADER_WAIT_SEC = 120.0
_MIN_429_WAIT_SEC = 2.0
_FALLBACK_429_BASE_SEC = 5.0


@dataclass(frozen=True)
class RateLimitHeaders:
    limit: int | None = None
    remaining: int | None = None
    """Unix epoch seconds when the window resets, if the vendor sent one."""
    reset_epoch: float | None = None
    retry_after_sec: float | None = None


def parse_rate_limit_headers(headers: Mapping[str, str]) -> RateLimitHeaders:
    """Read Alpaca-style rate headers. Missing fields stay None — never invent."""

    def _get(*names: str) -> str | None:
        # httpx Headers are case-insensitive; plain dicts in tests may not be.
        lower = {str(k).lower(): v for k, v in headers.items()}
        for name in names:
            raw = lower.get(name.lower())
            if raw is not None and str(raw).strip() != "":
                return str(raw).strip()
        return None

    def _int(raw: str | None) -> int | None:
        if raw is None:
            return None
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return None

    def _float(raw: str | None) -> float | None:
        if raw is None:
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    return RateLimitHeaders(
        limit=_int(_get("X-RateLimit-Limit", "X-Ratelimit-Limit")),
        remaining=_int(_get("X-RateLimit-Remaining", "X-Ratelimit-Remaining")),
        reset_epoch=_float(_get("X-RateLimit-Reset", "X-Ratelimit-Reset")),
        retry_after_sec=_float(_get("Retry-After")),
    )


def wait_seconds_from_headers(
    headers: RateLimitHeaders,
    *,
    now_epoch: float | None = None,
) -> float | None:
    """How long to sleep given vendor headers. None = headers do not say."""
    now = time.time() if now_epoch is None else now_epoch
    if headers.retry_after_sec is not None and headers.retry_after_sec >= 0:
        return min(_MAX_HEADER_WAIT_SEC, headers.retry_after_sec)
    if headers.reset_epoch is not None:
        return min(_MAX_HEADER_WAIT_SEC, max(0.0, headers.reset_epoch - now))
    return None


def fallback_429_wait(attempt: int) -> float:
    """When the vendor omits Reset/Retry-After — exponential with jitter."""
    base = _FALLBACK_429_BASE_SEC * (2 ** max(0, attempt))
    return float(min(_MAX_HEADER_WAIT_SEC, base + random.uniform(0.0, base * 0.25)))


class AccountQuota:
    """Shared quota for one vendor API key.

    Threaded through every paced request for that key. Safe across asyncio tasks
    in one process; multi-worker deploys are already refused elsewhere.
    """

    def __init__(self, rpm: float) -> None:
        rpm = max(1.0, float(rpm))
        self._rpm = rpm
        self._bucket = RateLimiter(rpm / 60.0, burst=max(2.0, rpm / 40.0))
        self._lock = asyncio.Lock()
        self._limit: int | None = None
        self._remaining: int | None = None
        self._reset_epoch: float | None = None
        self._cooldown_until_mono = 0.0
        self._throttled_total = 0
        self._observed_total = 0

    @property
    def rpm(self) -> float:
        return self._rpm

    def retarget(self, rpm: float) -> None:
        """Rebuild the floor bucket when config changes. Keeps header state."""
        rpm = max(1.0, float(rpm))
        if abs(rpm - self._rpm) < 1e-9:
            return
        self._rpm = rpm
        self._bucket = RateLimiter(rpm / 60.0, burst=max(2.0, rpm / 40.0))

    def observe(self, headers: Mapping[str, str], *, status_code: int | None = None) -> None:
        """Absorb rate headers from a response (success or 429)."""
        parsed = parse_rate_limit_headers(headers)
        self._observed_total += 1
        if parsed.limit is not None:
            self._limit = parsed.limit
        if parsed.remaining is not None:
            self._remaining = parsed.remaining
        if parsed.reset_epoch is not None:
            self._reset_epoch = parsed.reset_epoch
        if status_code == 429:
            self._throttled_total += 1

    async def note_throttled(
        self,
        headers: Mapping[str, str] | None,
        *,
        attempt: int = 0,
    ) -> float:
        """Record a 429 and return how long callers should wait before retry."""
        if headers is not None:
            self.observe(headers, status_code=429)
            parsed = parse_rate_limit_headers(headers)
            header_wait = wait_seconds_from_headers(parsed)
        else:
            self._throttled_total += 1
            header_wait = None

        wait = header_wait if header_wait is not None else fallback_429_wait(attempt)
        if header_wait is None:
            wait = max(_MIN_429_WAIT_SEC, wait)
        else:
            wait = max(0.0, wait)
        await self._bucket.penalize(wait)
        async with self._lock:
            self._cooldown_until_mono = max(
                self._cooldown_until_mono, time.monotonic() + wait
            )
            # Remaining is zero until the window resets — do not pretend otherwise.
            if self._remaining is None or self._remaining > 0:
                self._remaining = 0
        return wait

    def seconds_until_clear(self) -> float:
        """Scanner/desk: how long before it is safe to spend again."""
        now_mono = time.monotonic()
        cool = max(0.0, self._cooldown_until_mono - now_mono)
        if self._remaining is not None and self._remaining <= 0 and self._reset_epoch is not None:
            until_reset = self._reset_epoch - time.time()
            if until_reset > 0:
                cool = max(cool, min(_MAX_HEADER_WAIT_SEC, until_reset))
            else:
                self._remaining = None
        return cool

    def as_dict(self) -> dict[str, Any]:
        return {
            "rpm": self._rpm,
            "limit": self._limit,
            "remaining": self._remaining,
            "reset_epoch": self._reset_epoch,
            "cooldown_seconds": round(self.seconds_until_clear(), 2),
            "throttled_total": self._throttled_total,
            "observed_total": self._observed_total,
        }

    async def acquire(self) -> None:
        """Block until spending one request is within both floor and vendor state."""
        while True:
            async with self._lock:
                now_mono = time.monotonic()
                now_wall = time.time()
                if now_mono < self._cooldown_until_mono:
                    wait = self._cooldown_until_mono - now_mono
                elif (
                    self._remaining is not None
                    and self._remaining <= 0
                    and self._reset_epoch is not None
                ):
                    until_reset = self._reset_epoch - now_wall
                    if until_reset <= 0:
                        # Window already rolled — Remaining is stale until the
                        # next response. Do not spin on a past Reset.
                        self._remaining = None
                        wait = 0.0
                    else:
                        wait = min(_MAX_HEADER_WAIT_SEC, until_reset)
                elif self._soft_throttle_pause() > 0:
                    wait = self._soft_throttle_pause()
                else:
                    wait = 0.0
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            await self._bucket.acquire()
            return

    def _soft_throttle_pause(self) -> float:
        """Brief pause when Remaining is nearly gone — avoid earning the 429."""
        if self._remaining is None or self._limit is None or self._limit <= 0:
            return 0.0
        floor = max(_SOFT_REMAINING_FLOOR, int(self._limit * _SOFT_REMAINING_FRAC))
        if self._remaining > floor:
            return 0.0
        # Spread the last few requests across the rest of the window when known.
        if self._reset_epoch is not None and self._remaining > 0:
            left = max(0.0, self._reset_epoch - time.time())
            return min(2.0, left / max(1, self._remaining))
        return 60.0 / max(1.0, self._rpm)
