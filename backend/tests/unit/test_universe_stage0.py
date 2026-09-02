"""The universe service, and Stage 0's structural eligibility.

At 166 curated names, eligibility was a question nobody had to ask: every symbol
on the list was a large US listing because a person had put it there. Pointed at
a vendor's asset feed the same code sees 14,276 records, of which 1,146 are OTC,
871 are not tradable and several hundred are not common shares at all.

So these tests are about the difference between a symbol and an instrument.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from universe.eligibility import EligibilityPolicy, check_instrument, screen_universe
from universe.models import AssetClass, EligibilityReason, Instrument, UniverseTier
from universe.provider import StaticUniverseProvider, _instrument_from_alpaca
from universe.service import UniverseService, _cap_eligible, _merge_tier, _quality_key


def _instrument(symbol: str = "AAPL", **overrides: object) -> Instrument:
    base: dict[str, object] = {
        "symbol": symbol,
        "asset_class": AssetClass.STOCK,
        "exchange": "NASDAQ",
        "currency": "USD",
        "active": True,
        "tradable": True,
    }
    base.update(overrides)
    return Instrument(**base)  # type: ignore[arg-type]


# ── Stage 0 ─────────────────────────────────────────────────────────────────


def test_a_plain_us_listing_is_eligible() -> None:
    result = check_instrument(_instrument())
    assert result.eligible
    assert result.reasons == ()


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"otc": True}, EligibilityReason.OTC_BLOCKED),
        ({"exchange": "OTC"}, EligibilityReason.OTC_BLOCKED),
        ({"active": False}, EligibilityReason.INACTIVE),
        ({"tradable": False}, EligibilityReason.NOT_TRADABLE),
        ({"currency": "EUR"}, EligibilityReason.NON_USD),
        ({"asset_class": AssetClass.OTHER}, EligibilityReason.UNSUPPORTED_ASSET),
        ({"exchange": "TSX"}, EligibilityReason.UNSUPPORTED_EXCHANGE),
        ({"last_price": Decimal("1.20")}, EligibilityReason.PRICE_BELOW_MINIMUM),
    ],
)
def test_each_structural_defect_has_its_own_reason(overrides: dict, reason: str) -> None:
    """Codes, not sentences: these are counted and compared across cycles."""
    result = check_instrument(_instrument(**overrides))
    assert not result.eligible
    assert reason in result.reasons


def test_every_reason_is_reported_not_just_the_first() -> None:
    """A name refused twice over must not be filed under whichever ran first."""
    result = check_instrument(_instrument(otc=True, active=False, currency="CAD"))

    assert EligibilityReason.OTC_BLOCKED in result.reasons
    assert EligibilityReason.INACTIVE in result.reasons
    assert EligibilityReason.NON_USD in result.reasons


@pytest.mark.parametrize("symbol", ["BRK.B", "AAPL-W", "RDS/A", "TOOLONGXX", "SPX500"])
def test_non_common_share_identities_are_refused(symbol: str) -> None:
    """Warrants, units, rights and class lines arrive looking like tickers.

    They are not common shares, V1 has no sizing or exit model for them, and
    several would pass a liquidity test comfortably. Refused on identity, before
    a data slot is spent on them.
    """
    result = check_instrument(_instrument(symbol))

    assert not result.eligible
    assert EligibilityReason.INVALID_IDENTITY in result.reasons


def test_a_missing_reference_price_is_not_a_rejection() -> None:
    """Absence is not a penny stock. Stage 1 reads a real price for what remains."""
    assert check_instrument(_instrument(last_price=None)).eligible


def test_a_blocked_symbol_is_refused_without_looking_further() -> None:
    policy = EligibilityPolicy(blocked_symbols=frozenset({"AAPL"}))
    result = check_instrument(_instrument("AAPL"), policy)

    assert not result.eligible
    assert EligibilityReason.SYMBOL_BLOCKED in result.reasons


def test_screening_counts_reasons_across_the_universe() -> None:
    outcome = screen_universe(
        [
            _instrument("AAA"),
            _instrument("BBB", otc=True),
            _instrument("CCC", tradable=False),
            _instrument("DDD", otc=True),
        ]
    )

    assert [i.key for i in outcome.eligible] == ["AAA"]
    assert outcome.reason_counts[EligibilityReason.OTC_BLOCKED] == 2
    assert outcome.reason_counts[EligibilityReason.NOT_TRADABLE] == 1


# ── Provider normalisation ──────────────────────────────────────────────────


def test_an_alpaca_asset_record_becomes_an_instrument() -> None:
    instrument = _instrument_from_alpaca(
        {
            "symbol": "aapl",
            "class": "us_equity",
            "exchange": "NASDAQ",
            "status": "active",
            "tradable": True,
            "fractionable": True,
        }
    )
    assert instrument is not None
    assert instrument.symbol == "AAPL"
    assert instrument.asset_class is AssetClass.STOCK
    assert instrument.active and instrument.tradable


def test_an_asset_record_without_a_symbol_is_dropped() -> None:
    """Not admitted with a blank symbol, which would be fetched for and then
    rejected for a reason that says nothing about what went wrong."""
    assert _instrument_from_alpaca({"class": "us_equity"}) is None


def test_a_delisted_record_is_marked_inactive_not_discarded() -> None:
    """Live universe and research universe are different things.

    The instrument still exists as a record — Stage 0 refuses it for the live
    universe, and nothing has erased its identity for a backtest.
    """
    instrument = _instrument_from_alpaca(
        {"symbol": "DEAD", "class": "us_equity", "status": "inactive", "tradable": False}
    )
    assert instrument is not None
    assert instrument.active is False
    assert not check_instrument(instrument).eligible


# ── Tiers ───────────────────────────────────────────────────────────────────


def test_tiers_do_not_duplicate_symbols() -> None:
    curated = [_instrument("AAA"), _instrument("BBB")]
    provider = [_instrument("BBB"), _instrument("CCC")]

    merged = _merge_tier(provider, curated, tier=UniverseTier.EXTENDED, max_size=0)

    assert [i.key for i in merged] == ["AAA", "BBB", "CCC"]


def test_the_curated_record_wins_a_duplicate() -> None:
    """Its sector metadata is what correlation clustering reads."""
    curated = [_instrument("BBB", sector="technology")]
    provider = [_instrument("BBB", sector=None)]

    merged = _merge_tier(provider, curated, tier=UniverseTier.EXTENDED, max_size=0)

    assert merged[0].sector == "technology"


def test_merge_does_not_cap_before_eligibility() -> None:
    """Capacity is applied after Stage 0 — cutting here recreated the A–B universe."""
    provider = [_instrument(s) for s in ("ZZZ", "AAA", "MMM")]

    merged = _merge_tier(provider, [], tier=UniverseTier.BROAD, max_size=2)

    assert {i.key for i in merged} == {"AAA", "MMM", "ZZZ"}


def test_cap_keeps_curated_even_when_alphabet_would_drop_them() -> None:
    """EXTENDED used to sort then slice at 2000 — NVDA never made the cut."""
    curated = [
        _instrument("NVDA", exchange="NASDAQ", fractionable=True),
        _instrument("MSFT", exchange="NASDAQ", fractionable=True),
        _instrument("KO", exchange="NYSE", fractionable=True),
    ]
    # Many early-alphabet shells that used to consume the entire budget.
    junk = [_instrument(f"A{i:03d}", exchange="NASDAQ", fractionable=False) for i in range(50)]
    eligible = curated + junk
    curated_keys = frozenset(i.key for i in curated)

    capped = _cap_eligible(eligible, curated_keys=curated_keys, max_size=10)

    keys = {i.key for i in capped}
    assert {"NVDA", "MSFT", "KO"} <= keys
    assert len(capped) == 10


def test_cap_fills_remaining_slots_with_higher_quality_names() -> None:
    """Major-exchange fractionable shorts beat thin long tickers."""
    curated_keys: frozenset[str] = frozenset()
    eligible = [
        _instrument("AAAAA", exchange="IEX", fractionable=False),
        _instrument("IBM", exchange="NYSE", fractionable=True),
        _instrument("BBBBB", exchange="IEX", fractionable=False),
        _instrument("CAT", exchange="NYSE", fractionable=True),
    ]

    capped = _cap_eligible(eligible, curated_keys=curated_keys, max_size=2)

    assert [i.key for i in capped] == ["CAT", "IBM"]


def test_quality_key_prefers_nyse_over_unknown_venue() -> None:
    nyse = _instrument("AAA", exchange="NYSE", fractionable=True)
    unknown = _instrument("AA", exchange="DARK", fractionable=True)
    assert _quality_key(nyse) < _quality_key(unknown)


# ── Caching ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reference_data_is_not_refetched_every_cycle() -> None:
    """Fourteen thousand asset records every five minutes, to learn nothing."""
    provider = _CountingProvider()
    service = UniverseService(provider, curated_provider=provider)

    await service.get_universe(tier=UniverseTier.CORE)
    await service.get_universe(tier=UniverseTier.CORE)

    assert provider.calls == 1


@pytest.mark.asyncio
async def test_a_changed_policy_invalidates_the_cache() -> None:
    """A cached verdict computed under a different policy is wrong, not stale.

    Without the input version in the cache key the desk would enforce
    yesterday's rules while showing today's configuration.
    """
    provider = _CountingProvider()
    service = UniverseService(provider, curated_provider=provider)

    first = await service.get_universe(tier=UniverseTier.CORE, max_size=0)
    second = await service.get_universe(tier=UniverseTier.CORE, max_size=1)

    assert provider.calls == 2
    assert len(first.eligible) != len(second.eligible)


@pytest.mark.asyncio
async def test_an_expired_snapshot_is_refetched() -> None:
    provider = _CountingProvider()
    service = UniverseService(provider, curated_provider=provider, refresh_sec=0.0)

    await service.get_universe(tier=UniverseTier.CORE)
    await service.get_universe(tier=UniverseTier.CORE)

    assert provider.calls == 2


@pytest.mark.asyncio
async def test_a_new_listing_appears_after_a_refresh() -> None:
    """Universe refresh has to see new listings, or the desk trades yesterday's market."""
    provider = _CountingProvider()
    service = UniverseService(provider, curated_provider=provider, refresh_sec=0.0)

    before = await service.get_universe(tier=UniverseTier.CORE)
    provider.instruments.append(_instrument("NEWW"))
    after = await service.get_universe(tier=UniverseTier.CORE)

    assert "NEWW" not in before.symbols
    assert "NEWW" in after.symbols


@pytest.mark.asyncio
async def test_the_snapshot_reports_its_own_stage0_accounting() -> None:
    provider = StaticUniverseProvider([_instrument("AAA"), _instrument("BBB", otc=True)], name="t")
    service = UniverseService(provider, curated_provider=provider)

    snapshot = await service.get_universe(tier=UniverseTier.CORE)

    assert snapshot.total == 2
    assert snapshot.rejected_count == 1
    assert snapshot.rejection_reasons[EligibilityReason.OTC_BLOCKED] == 1


class _CountingProvider:
    def __init__(self) -> None:
        self.calls = 0
        self.instruments = [_instrument("AAA"), _instrument("BBB")]

    @property
    def name(self) -> str:
        return "counting"

    async def get_universe(self, *, tier: UniverseTier = UniverseTier.CORE) -> list[Instrument]:
        self.calls += 1
        return list(self.instruments)


def test_an_instrument_carries_when_its_reference_data_was_read() -> None:
    """Reference data is slow, not static; an undated record cannot be aged."""
    now = datetime.now(UTC)
    assert _instrument(as_of=now).as_of == now
