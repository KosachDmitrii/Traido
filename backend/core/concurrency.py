"""Bounded concurrency and provider rate budgets.

The scanner used to be safe from rate limits by being slow: one symbol at a
time, with a fixed 0.4-second pause. That is a throughput ceiling disguised as a
policy, and it does not survive a universe of a thousand names.

Replacing it with `asyncio.gather` over everything would replace one problem
with a worse one — a burst of a thousand concurrent requests is how an account
gets throttled for the rest of the session, and a 429 storm looks exactly like a
provider outage to every fail-closed gate downstream.

So: a fixed number of workers per resource, and a token bucket per provider. The
two are separate on purpose. Concurrency bounds how many requests are in flight;
the rate bucket bounds how many are started per second. A pool of eight against
a provider allowing three per second still needs the bucket, because eight fast
responses simply come back sooner and start eight more.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field


class RateLimiter:
    """A token bucket, in monotonic time.

    Allows a burst up to `burst` and then settles to `rate_per_sec`. Bursting is
    wanted: batched reads arrive as a handful of large requests, and forcing
    them to trickle would add latency for no protection.
    """

    def __init__(self, rate_per_sec: float, *, burst: float | None = None) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self._rate = rate_per_sec
        self._capacity = burst if burst is not None else max(1.0, rate_per_sec)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self._penalty_until = 0.0

    async def acquire(self, tokens: float = 1.0) -> None:
        while True:
            async with self._lock:
                now = time.monotonic()
                if now < self._penalty_until:
                    wait = self._penalty_until - now
                else:
                    self._tokens = min(
                        self._capacity, self._tokens + (now - self._updated) * self._rate
                    )
                    self._updated = now
                    if self._tokens >= tokens:
                        self._tokens -= tokens
                        return
                    deficit = tokens - self._tokens
                    wait = deficit / self._rate
            await asyncio.sleep(wait)

    async def penalize(self, seconds: float) -> None:
        """Hold the bucket after a 429 — do not refill into a hot window."""
        async with self._lock:
            now = time.monotonic()
            self._tokens = 0.0
            self._updated = now
            self._penalty_until = max(self._penalty_until, now + max(0.0, seconds))


@dataclass
class ResourceBudget:
    """What one class of work is allowed to consume.

    Named per resource rather than per provider because the limits that bite are
    not all the vendor's: LLM concurrency is about cost, bar concurrency is
    about the data API's quota, and broker concurrency is about not confusing an
    adapter that holds a session.
    """

    name: str
    max_concurrency: int = 8
    rate_per_sec: float | None = None
    burst: float | None = None
    timeout_sec: float = 60.0


DEFAULT_BUDGETS: dict[str, ResourceBudget] = {
    # Reference data is one big request, rarely.
    "reference": ResourceBudget("reference", max_concurrency=2, rate_per_sec=2.0),
    # Batched snapshots and daily bars: few requests, each large.
    "market_data": ResourceBudget("market_data", max_concurrency=2, rate_per_sec=2.0),
    # Per-symbol deep analysis. Counts *symbols*, not requests: one symbol
    # paginates hourly bars a dozen times. Concurrent deep symbols multiply
    # that into a 429 storm against the shared account quota — keep at one.
    "deep": ResourceBudget("deep", max_concurrency=1, rate_per_sec=1.0, timeout_sec=90.0),
    "news": ResourceBudget("news", max_concurrency=2, rate_per_sec=2.0),
    "fundamentals": ResourceBudget("fundamentals", max_concurrency=2, rate_per_sec=1.0),
    "broker": ResourceBudget("broker", max_concurrency=1, rate_per_sec=4.0),
    # Deliberately the tightest: an LLM call costs money as well as time.
    "llm": ResourceBudget("llm", max_concurrency=2, rate_per_sec=1.0),
}


@dataclass
class _Gate:
    semaphore: asyncio.Semaphore
    limiter: RateLimiter | None
    budget: ResourceBudget


@dataclass
class ProviderStats:
    """What a cycle actually spent. Reported, not merely counted."""

    calls: int = 0
    failures: int = 0
    timeouts: int = 0
    seconds: float = 0.0


class ConcurrencyManager:
    """Gates work by resource class, and records what each one cost.

    Created per scan cycle, so the semaphores cannot leak across cycles and the
    statistics describe one cycle rather than the life of the process.
    """

    def __init__(self, budgets: dict[str, ResourceBudget] | None = None) -> None:
        self._budgets = dict(budgets or DEFAULT_BUDGETS)
        self._gates: dict[str, _Gate] = {}
        self.stats: dict[str, ProviderStats] = {}

    def _gate(self, resource: str) -> _Gate:
        gate = self._gates.get(resource)
        if gate is None:
            budget = self._budgets.get(resource) or ResourceBudget(resource)
            gate = _Gate(
                semaphore=asyncio.Semaphore(max(1, budget.max_concurrency)),
                limiter=(
                    RateLimiter(budget.rate_per_sec, burst=budget.burst)
                    if budget.rate_per_sec
                    else None
                ),
                budget=budget,
            )
            self._gates[resource] = gate
            self.stats.setdefault(resource, ProviderStats())
        return gate

    async def run[T](self, resource: str, coro_factory: Callable[[], Awaitable[T]]) -> T:
        """Run one unit of work under this resource's budget.

        Takes a factory rather than a coroutine because a coroutine created
        eagerly and then queued behind a semaphore has already been constructed;
        for a cancelled or timed-out batch that leaves un-awaited coroutines and
        a warning per symbol.
        """
        gate = self._gate(resource)
        stats = self.stats.setdefault(resource, ProviderStats())
        async with gate.semaphore:
            if gate.limiter is not None:
                await gate.limiter.acquire()
            started = time.monotonic()
            stats.calls += 1
            try:
                return await asyncio.wait_for(coro_factory(), timeout=gate.budget.timeout_sec)
            except TimeoutError:
                stats.timeouts += 1
                stats.failures += 1
                raise
            except Exception:
                stats.failures += 1
                raise
            finally:
                stats.seconds += time.monotonic() - started

    async def map[TIn, TOut](
        self,
        resource: str,
        items: Iterable[TIn],
        worker: Callable[[TIn], Awaitable[TOut]],
    ) -> list[TOut | BaseException]:
        """Run `worker` over `items` under the budget, keeping input order.

        Results come back positionally, and a failure is returned as its
        exception rather than raised. One symbol's provider error must not kill
        a scan — and it must not vanish either, which is why the exception is a
        value the caller has to account for.
        """
        materialised = list(items)

        async def _one(item: TIn) -> TOut | BaseException:
            try:
                return await self.run(resource, lambda: worker(item))
            except Exception as exc:  # noqa: BLE001
                return exc

        return list(await asyncio.gather(*(_one(item) for item in materialised)))

    def as_dict(self) -> dict[str, dict[str, float]]:
        return {
            name: {
                "calls": s.calls,
                "failures": s.failures,
                "timeouts": s.timeouts,
                "seconds": round(s.seconds, 3),
            }
            for name, s in sorted(self.stats.items())
            if s.calls
        }


@dataclass
class AIBudget:
    """What one scan may spend on the LLM, before it spends any of it.

    There is no LLM on the scan path today — the audit confirmed zero Anthropic
    calls — so this is a limit built before the thing it limits. That is the
    order that works: the first cycle that sends a thousand symbols to Claude is
    not the moment to discover there was no ceiling.

    When the budget runs out, remaining candidates are not dropped and not
    chosen at random. They are refused in deterministic pre-ranking order and
    counted as `AI_BUDGET_EXHAUSTED`, so the shortlist stays reproducible and
    the funnel still adds up.
    """

    max_candidates: int = 20
    max_calls: int = 60
    max_tokens: int | None = None
    max_cost_usd: float | None = None

    candidates_used: int = 0
    calls_used: int = 0
    tokens_used: int = 0
    cost_used: float = 0.0
    exhausted_candidates: list[str] = field(default_factory=list)

    def may_take_candidate(self) -> bool:
        if self.candidates_used >= self.max_candidates:
            return False
        if self.calls_used >= self.max_calls:
            return False
        if self.max_tokens is not None and self.tokens_used >= self.max_tokens:
            return False
        return not (self.max_cost_usd is not None and self.cost_used >= self.max_cost_usd)

    def take_candidate(self, symbol: str) -> bool:
        if not self.may_take_candidate():
            self.exhausted_candidates.append(symbol)
            return False
        self.candidates_used += 1
        return True

    def record_call(self, *, tokens: int = 0, cost_usd: float = 0.0) -> None:
        self.calls_used += 1
        self.tokens_used += tokens
        self.cost_used += cost_usd

    def as_dict(self) -> dict[str, float | int]:
        return {
            "max_candidates": self.max_candidates,
            "candidates_used": self.candidates_used,
            "max_calls": self.max_calls,
            "calls_used": self.calls_used,
            "tokens_used": self.tokens_used,
            "cost_used": round(self.cost_used, 4),
            "exhausted": len(self.exhausted_candidates),
        }
