"""Stage 0 — is this thing something the desk may look at at all?

Cheap, deterministic, and answered from reference data that changes on the scale
of days. No market data, no broker, no LLM: this stage runs over the entire
universe, so anything it touches is touched a thousand times.

The policy is deliberately the same shape as `trading/gates.py`
`check_instrument_eligibility`, which asks the same questions of one symbol at
execution time. Both must agree; a name that Stage 0 admits and the execution
gate refuses is a wasted cycle, and a name Stage 0 admits that the execution
gate *should* refuse but does not is a hole. Stage 0 is the wider net of the
two by construction — it runs on reference data, which is older and coarser
than what the execution gate sees.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from universe.models import (
    AssetClass,
    EligibilityReason,
    Instrument,
    UniverseEligibilityResult,
)

TRADABLE_ASSET_CLASSES = frozenset({AssetClass.STOCK, AssetClass.ETF})

SUPPORTED_EXCHANGES = frozenset(
    {
        "NASDAQ",
        "NYSE",
        "NYSEARCA",
        "ARCA",
        "AMEX",
        "NYSEAMERICAN",
        "BATS",
        "IEX",
        "CBOE",
        "OTC",  # named so it can be *recognised* and then rejected as OTC
    }
)
"""Venues whose listings V1 understands.

An unknown exchange is rejected rather than admitted. A venue nobody has
considered is a venue whose settlement, tick rules and halt semantics nobody has
considered either, and the sizing and stop model assume a US listed share.
"""

OTC_EXCHANGES = frozenset({"OTC", "OTCM", "PINK", "OTCBB", "GREY", "EXPERT"})


@dataclass(frozen=True)
class EligibilityPolicy:
    """The thresholds Stage 0 applies. Configuration, not code.

    `min_price` exists because sizing, stops and slippage all assume a share
    whose tick is small relative to its price. A two-dollar stock clears every
    liquidity test on printed volume and has no book to exit into.
    """

    min_price: Decimal = Decimal(5)
    max_price: Decimal | None = Decimal(10000)
    allow_otc: bool = False
    allowed_asset_classes: frozenset[AssetClass] = TRADABLE_ASSET_CLASSES
    allowed_exchanges: frozenset[str] = SUPPORTED_EXCHANGES
    currency: str = "USD"
    blocked_symbols: frozenset[str] = frozenset()
    corporate_action_blocked: frozenset[str] = frozenset()
    require_tradable: bool = True

    def replace(self, **changes: object) -> EligibilityPolicy:
        from dataclasses import replace as _replace

        return _replace(self, **changes)  # type: ignore[arg-type]


def _identity_is_plain(symbol: str) -> bool:
    """A plain US common-stock or ETF ticker, and nothing more exotic.

    Warrants, units, rights and preferred lines all reach a vendor asset feed as
    ordinary-looking strings with a suffix. They are not common shares and V1
    has no model for them, so they are refused on identity rather than left to
    be caught by a liquidity test they may well pass.
    """
    if not symbol or len(symbol) > 6:
        return False
    return symbol.isupper() and symbol.isalpha()


def check_instrument(
    instrument: Instrument,
    policy: EligibilityPolicy | None = None,
) -> UniverseEligibilityResult:
    """Every reason it fails, not the first one.

    Collecting all of them costs nothing here and makes the funnel honest: a
    name refused for being both OTC and inactive should not be filed under
    whichever check happened to run first.
    """
    pol = policy or EligibilityPolicy()
    reasons: list[str] = []
    symbol = instrument.key

    if not _identity_is_plain(symbol):
        reasons.append(EligibilityReason.INVALID_IDENTITY)
    if symbol in pol.blocked_symbols:
        reasons.append(EligibilityReason.SYMBOL_BLOCKED)
    if symbol in pol.corporate_action_blocked:
        reasons.append(EligibilityReason.CORPORATE_ACTION_BLOCK)
    if instrument.asset_class not in pol.allowed_asset_classes:
        reasons.append(EligibilityReason.UNSUPPORTED_ASSET)
    if not instrument.active:
        reasons.append(EligibilityReason.INACTIVE)
    if pol.require_tradable and not instrument.tradable:
        reasons.append(EligibilityReason.NOT_TRADABLE)
    if instrument.currency.upper() != pol.currency.upper():
        reasons.append(EligibilityReason.NON_USD)

    exchange = (instrument.exchange or "").upper()
    if exchange and exchange not in pol.allowed_exchanges:
        reasons.append(EligibilityReason.UNSUPPORTED_EXCHANGE)
    if not pol.allow_otc and (instrument.otc or exchange in OTC_EXCHANGES):
        reasons.append(EligibilityReason.OTC_BLOCKED)

    # Reference price may be absent, and absence is not a rejection: Stage 1
    # reads a real price for everything that gets this far. It is used here only
    # when present, to drop obvious penny names before they cost a data slot.
    price = instrument.last_price
    if price is not None:
        if price < pol.min_price:
            reasons.append(EligibilityReason.PRICE_BELOW_MINIMUM)
        elif pol.max_price is not None and price > pol.max_price:
            reasons.append(EligibilityReason.PRICE_ABOVE_MAXIMUM)

    return UniverseEligibilityResult(
        instrument=instrument,
        eligible=not reasons,
        reasons=tuple(reasons),
    )


@dataclass
class EligibilityOutcome:
    """Both sides of the Stage 0 split, so nothing has to be recomputed."""

    eligible: list[Instrument] = field(default_factory=list)
    rejected: list[UniverseEligibilityResult] = field(default_factory=list)

    @property
    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.rejected:
            for reason in result.reasons:
                counts[reason] = counts.get(reason, 0) + 1
        return counts


def screen_universe(
    instruments: Iterable[Instrument],
    policy: EligibilityPolicy | None = None,
) -> EligibilityOutcome:
    """Stage 0 over a whole universe. Deterministic and order-preserving."""
    pol = policy or EligibilityPolicy()
    outcome = EligibilityOutcome()
    for instrument in instruments:
        result = check_instrument(instrument, pol)
        if result.eligible:
            outcome.eligible.append(instrument)
        else:
            outcome.rejected.append(result)
    return outcome
