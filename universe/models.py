"""What a tradable instrument is, before anyone has looked at its price.

The scan path had no notion of instrument identity at all: it took a list of
strings from a JSON file and started fetching bars for them. That is survivable
for 166 hand-curated names and unsafe for a thousand from a vendor's asset feed,
where OTC shells, non-USD lines, halted listings and preferred-share tickers all
arrive looking exactly like a symbol.

`check_instrument_eligibility` in `trading/gates.py` already asks these questions
at execution. It asks them of one symbol about to be traded. This asks them of
everything the desk is willing to *analyse*, which is a different and much
larger set, and it asks them from reference data rather than from a ticker's
spelling.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import Field

from core.schemas import StrictModel


class AssetClass(StrEnum):
    """What kind of thing this is. V1 trades the first two and nothing else."""

    STOCK = "stock"
    ETF = "etf"
    OTHER = "other"
    """Anything the provider offers that V1 has no sizing or exit model for.

    Deliberately one bucket rather than an enumeration of the things we do not
    trade. A new instrument type appearing at the vendor must land somewhere
    that fails the eligibility check, not somewhere that has no branch and is
    therefore allowed through.
    """


class UniverseTier(StrEnum):
    """How wide the desk is willing to look.

    Tiers are about *scope of the source*, not about quality: a name's liquidity
    is measured in Stage 1 from real market data, never assumed from the tier it
    arrived in.
    """

    CORE = "core"
    """The curated list in `configs/universe.json`. What the desk ran before."""

    EXTENDED = "extended"
    """Curated names first, then the provider's, up to the configured cap."""

    BROAD = "broad"
    """Everything the provider lists that survives structural eligibility."""


class Instrument(StrictModel):
    """One line at one venue, with the facts needed to decide whether to look.

    Reference data only. Nothing here is a price you may trade on: `last_price`
    is whatever the reference feed happened to carry and may be days old, which
    is why it is allowed to reject a penny stock and never allowed to size a
    position.
    """

    symbol: str = Field(min_length=1, max_length=16)
    asset_class: AssetClass = AssetClass.OTHER
    exchange: str | None = None
    currency: str = "USD"

    active: bool = True
    """The listing exists today. False for delisted names kept for research."""

    tradable: bool = True
    """The venue will accept an order. A name can be active and not tradable."""

    otc: bool = False
    shortable: bool | None = None
    fractionable: bool | None = None

    last_price: Decimal | None = None
    market_cap: Decimal | None = None

    sector: str | None = None
    provider: str = "unknown"
    """Which source asserted all of the above. Kept so a bad feed is nameable."""

    as_of: datetime | None = None
    """When the reference record was read. Reference data is slow, not static."""

    metadata: dict[str, str] = Field(default_factory=dict)

    @property
    def key(self) -> str:
        return self.symbol.upper()


class EligibilityReason(StrEnum):
    """Why a name may not be analysed. Every rejection names one of these.

    Reason codes rather than free text because these are counted in the funnel
    and compared across cycles. A message that reads well once is useless as a
    key.
    """

    UNSUPPORTED_ASSET = "UNSUPPORTED_ASSET"
    OTC_BLOCKED = "OTC_BLOCKED"
    INACTIVE = "INACTIVE"
    NOT_TRADABLE = "NOT_TRADABLE"
    NON_USD = "NON_USD"
    UNSUPPORTED_EXCHANGE = "UNSUPPORTED_EXCHANGE"
    PRICE_BELOW_MINIMUM = "PRICE_BELOW_MINIMUM"
    PRICE_ABOVE_MAXIMUM = "PRICE_ABOVE_MAXIMUM"
    CORPORATE_ACTION_BLOCK = "CORPORATE_ACTION_BLOCK"
    SYMBOL_BLOCKED = "SYMBOL_BLOCKED"
    INVALID_IDENTITY = "INVALID_IDENTITY"


class UniverseEligibilityResult(StrictModel):
    """One instrument's verdict, with the reasons attached to it.

    Carries the instrument rather than just the symbol so a rejection can be
    inspected without going back to the provider for what it said.
    """

    instrument: Instrument
    eligible: bool
    reasons: tuple[str, ...] = ()

    @property
    def symbol(self) -> str:
        return self.instrument.key
