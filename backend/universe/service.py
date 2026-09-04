"""The universe the desk is willing to look at, assembled once and cached.

Reference data changes on the scale of a trading day: a new listing appears, a
name is halted, a ticker changes hands. Fetching it per cycle would be a
thousand-record download every five minutes to learn nothing. Fetching it once
and never again would mean trading a delisted symbol.

So it is cached with an explicit refresh interval, and the cached value carries
the policy version it was screened under — change the eligibility policy and the
cache is not merely stale, it is *wrong*, and must not be served.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from core.freshness import Cached, FreshnessCache
from universe.eligibility import EligibilityOutcome, EligibilityPolicy, screen_universe
from universe.models import Instrument, UniverseTier

DEFAULT_REFRESH_SEC = 6 * 3600.0
"""How long a reference snapshot is good for.

Six hours, so a scan started at any point in a session is working from data read
at latest before the previous close. New listings are not tradable on day one
under this policy anyway — they have no ADV history for Stage 1 to measure.
"""

SELECTION_VERSION = "quality_v1"
"""Bumped when the post-screen ranking/cap rule changes.

Without this in the cache key, a six-hour snapshot built under the old
alphabetical A–B cut would keep starving the desk of NVDA after a deploy.
"""

# Major listed venues first. Unknown / thin venues last. OTC never wins a slot
# when a NYSE/NASDAQ name is still available — Stage 0 already refuses OTC for
# the live universe, but ranking must agree if policy is ever loosened.
_EXCHANGE_RANK: dict[str, int] = {
    "NYSE": 0,
    "NASDAQ": 0,
    "ARCA": 1,
    "NYSEARCA": 1,
    "AMEX": 2,
    "NYSEAMERICAN": 2,
    "BATS": 3,
    "IEX": 3,
    "CBOE": 3,
}


@dataclass
class UniverseSnapshot:
    """One resolved universe, with the accounting Stage 0 produced.

    `rejected` is kept rather than discarded because the funnel has to explain
    where every name went, and "800 of 1500 were structurally eligible" is only
    half an answer without the reasons for the other 700.

    `capped_out` is Stage-0-eligible names cut by `max_size` after screening.
    Without it the funnel cannot balance: the universe total still includes
    them, but they never enter market filter / deep analysis.
    """

    tier: UniverseTier
    provider: str
    total: int
    eligible: list[Instrument] = field(default_factory=list)
    rejected_count: int = 0
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    capped_out: int = 0

    @property
    def symbols(self) -> list[str]:
        return [i.key for i in self.eligible]


def _policy_version(policy: EligibilityPolicy, tier: UniverseTier, max_size: int) -> str:
    """A fingerprint of everything that could change the answer.

    Includes the tier, the size cap, and the selection algorithm version as well
    as the policy, because all of them select which instruments come back.
    Hashed rather than concatenated only to keep it a manageable cache key; it
    is not a security boundary.
    """
    raw = "|".join(
        [
            tier.value,
            str(max_size),
            SELECTION_VERSION,
            str(policy.min_price),
            str(policy.max_price),
            str(policy.allow_otc),
            ",".join(sorted(c.value for c in policy.allowed_asset_classes)),
            ",".join(sorted(policy.allowed_exchanges)),
            policy.currency,
            ",".join(sorted(policy.blocked_symbols)),
            ",".join(sorted(policy.corporate_action_blocked)),
            str(policy.require_tradable),
        ]
    )
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _quality_key(instrument: Instrument) -> tuple:
    """Cheapest available proxy for "worth a scan slot" without market data.

    Real ADV / liquidity is Stage 1's job and needs bars. At universe build we
    only have the asset feed, so rank by venue quality, fractionability (Alpaca's
    weak large-name proxy), ticker length (AAAA-style shells tend to be longer),
    then symbol for a stable tie-break. Lower tuple sorts first.
    """
    exchange = (instrument.exchange or "").upper()
    exchange_rank = _EXCHANGE_RANK.get(exchange, 8)
    # Prefer fractionable when the feed says so; unknown ties with True so a
    # curated record without the flag is not punished relative to Alpaca's.
    frac = instrument.fractionable
    frac_rank = 0 if frac is None or frac else 1
    length = len(instrument.key)
    return (exchange_rank, frac_rank, length, instrument.key)


def _merge_tier(
    provider_instruments: list[Instrument],
    curated: list[Instrument],
    *,
    tier: UniverseTier,
    max_size: int,
) -> list[Instrument]:
    """Assemble the tier, deduplicated by symbol.

    Cap is *not* applied here. Capping before Stage 0 eligibility was how the
    desk ended up scanning only A–B: fourteen thousand Alpaca names, sorted,
    sliced at 2000, then screened — the cap had already thrown away NVDA.
    Dedup keeps the first occurrence so curated sector metadata wins.
    """
    del max_size  # applied after screen in `_cap_eligible`
    if tier is UniverseTier.CORE:
        chosen = curated
    elif tier is UniverseTier.EXTENDED:
        chosen = curated + provider_instruments
    else:
        chosen = provider_instruments or curated

    seen: set[str] = set()
    out: list[Instrument] = []
    for instrument in chosen:
        key = instrument.key
        if key in seen:
            continue
        seen.add(key)
        out.append(instrument)
    return out


def _cap_eligible(
    eligible: list[Instrument],
    *,
    curated_keys: frozenset[str],
    max_size: int,
) -> list[Instrument]:
    """Keep every curated survivor, fill the rest with the best remaining names.

    `max_size <= 0` means no capacity budget — return everyone Stage 0 passed.
    Curated names that survived eligibility are never dropped to make room for a
    higher-ranked stranger; if curated alone exceeds the cap, curated wins and
    the discovery fill is empty.
    """
    if max_size <= 0:
        return sorted(eligible, key=_quality_key)

    must_keep = [i for i in eligible if i.key in curated_keys]
    discovery = [i for i in eligible if i.key not in curated_keys]
    discovery.sort(key=_quality_key)

    # Curated that survived Stage 0 always beat discovery names. If curated
    # alone exceeds the budget, keep the best curated slice rather than the
    # whole curated book — CORE with a tiny max_size must still mean something.
    if len(must_keep) >= max_size:
        return sorted(must_keep, key=_quality_key)[:max_size]

    remaining = max_size - len(must_keep)
    selected = must_keep + discovery[:remaining]
    # Stable display order for the desk watchlist: quality then symbol.
    return sorted(selected, key=_quality_key)


class UniverseService:
    """Resolve, screen and cache the universe.

    Holds no market data and makes no trading decision. Its whole job is to turn
    "what may we look at today" into a list, once, cheaply enough that the
    answer can be asked for on every cycle.
    """

    def __init__(
        self,
        provider,  # UniverseProvider; untyped to keep the protocol import optional
        *,
        curated_provider=None,
        policy: EligibilityPolicy | None = None,
        refresh_sec: float = DEFAULT_REFRESH_SEC,
    ) -> None:
        self._provider = provider
        self._curated = curated_provider
        self._policy = policy or EligibilityPolicy()
        self._refresh_sec = refresh_sec
        self._cache: FreshnessCache[UniverseSnapshot] = FreshnessCache()

    @property
    def policy(self) -> EligibilityPolicy:
        return self._policy

    def cached_entry(self, tier: UniverseTier) -> Cached[UniverseSnapshot] | None:
        """For reporting how old the universe is. Never used to decide."""
        return self._cache.peek(tier.value)

    async def get_universe(
        self,
        *,
        tier: UniverseTier = UniverseTier.CORE,
        max_size: int = 0,
        force: bool = False,
    ) -> UniverseSnapshot:
        version = _policy_version(self._policy, tier, max_size)
        if not force:
            hit = self._cache.get(tier.value, input_version=version)
            if hit is not None:
                return hit.value

        curated: list[Instrument] = []
        if tier is not UniverseTier.BROAD or self._curated is not None:
            source = self._curated or self._provider
            if tier is UniverseTier.CORE and self._curated is None:
                curated = await self._provider.get_universe(tier=tier)
            else:
                curated = await source.get_universe(tier=UniverseTier.CORE)

        provider_instruments: list[Instrument] = []
        if tier is not UniverseTier.CORE:
            provider_instruments = await self._provider.get_universe(tier=tier)

        # Merge without capacity cut, screen the full set, then keep curated and
        # the best remaining names up to max_size. Cutting before Stage 0 made
        # the live EXTENDED universe alphabetical A–B and dropped MSFT/NVDA.
        merged = _merge_tier(provider_instruments, curated, tier=tier, max_size=0)
        outcome: EligibilityOutcome = screen_universe(merged, self._policy)
        curated_keys = frozenset(i.key for i in curated)
        screened = outcome.eligible
        eligible = _cap_eligible(
            screened,
            curated_keys=curated_keys,
            max_size=max_size,
        )
        capped_out = max(0, len(screened) - len(eligible))

        snapshot = UniverseSnapshot(
            tier=tier,
            provider=getattr(self._provider, "name", "unknown"),
            total=len(merged),
            eligible=eligible,
            rejected_count=len(outcome.rejected),
            rejection_reasons=outcome.reason_counts,
            capped_out=capped_out,
        )
        self._cache.put(
            tier.value,
            snapshot,
            ttl_sec=self._refresh_sec,
            input_version=version,
        )
        return snapshot
