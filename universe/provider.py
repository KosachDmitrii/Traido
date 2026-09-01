"""Where the list of instruments comes from.

Scanner code must never contain a list of symbols, and must never know which
vendor produced one. It asks a `UniverseProvider` and gets `Instrument`s back.

Three implementations, none of which the scanner names: the curated JSON the
desk ran before, Alpaca's asset feed, and a static one for tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, runtime_checkable

import httpx

from core.config import Settings, get_settings
from core.universe import ETF_SECTOR, default_universe
from core.vendor_http import get_with_retry
from universe.models import AssetClass, Instrument, UniverseTier


@runtime_checkable
class UniverseProvider(Protocol):
    """A source of instruments.

    `get_universe` returns everything the source knows about that could
    plausibly be traded. It does *not* apply eligibility policy — that is
    Stage 0's job, and keeping it out of the provider means a provider can be
    swapped without the policy changing underneath it.
    """

    @property
    def name(self) -> str: ...

    async def get_universe(self, *, tier: UniverseTier = UniverseTier.CORE) -> list[Instrument]: ...


class StaticUniverseProvider:
    """A fixed list. Used by tests, and as the CORE tier in production.

    The curated `configs/universe.json` names carry sector metadata the risk
    engine's correlation clustering already depends on, which is why CORE is not
    simply "the first 166 things Alpaca lists".
    """

    def __init__(
        self,
        instruments: list[Instrument] | None = None,
        *,
        name: str = "static",
    ) -> None:
        self._instruments = instruments if instruments is not None else _curated_instruments()
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def get_universe(self, *, tier: UniverseTier = UniverseTier.CORE) -> list[Instrument]:
        return list(self._instruments)


def _curated_instruments() -> list[Instrument]:
    """The hand-maintained list, given instrument identity it never had.

    These are large US listings by construction, so the reference facts are
    asserted rather than fetched. Marked with this provider name so that if one
    of them is ever wrong, it is obvious where the assertion came from.
    """
    uni = default_universe()
    now = datetime.now(UTC)
    out: list[Instrument] = []
    for symbol in uni.symbols:
        sector = uni.sector_of(symbol)
        out.append(
            Instrument(
                symbol=symbol,
                asset_class=AssetClass.ETF if sector == ETF_SECTOR else AssetClass.STOCK,
                exchange=None,  # unknown, and an unknown exchange is not rejected
                currency="USD",
                active=True,
                tradable=True,
                otc=False,
                sector=None if sector == ETF_SECTOR else sector,
                provider="curated",
                as_of=now,
            )
        )
    return out


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


_ALPACA_CLASS = {
    "us_equity": AssetClass.STOCK,
}


def _instrument_from_alpaca(raw: dict[str, Any]) -> Instrument | None:
    """One asset record, or None if it is not even shaped like one.

    A record without a symbol is dropped rather than admitted with a blank one:
    it would be counted in the funnel, fetched for, and rejected later for a
    reason that says nothing about what actually went wrong.
    """
    symbol = str(raw.get("symbol") or "").strip().upper()
    if not symbol:
        return None

    exchange = str(raw.get("exchange") or "").strip().upper()
    asset_class = _ALPACA_CLASS.get(str(raw.get("class") or ""), AssetClass.OTHER)

    # Alpaca does not label ETFs as a class; it flags them in `attributes`.
    attributes = raw.get("attributes") or []
    if asset_class is AssetClass.STOCK and "etf" in {str(a).lower() for a in attributes}:
        asset_class = AssetClass.ETF

    return Instrument(
        symbol=symbol,
        asset_class=asset_class,
        exchange=exchange or None,
        currency="USD",  # Alpaca's US equity feed is USD by construction
        active=str(raw.get("status") or "").lower() == "active",
        tradable=bool(raw.get("tradable", False)),
        otc=exchange == "OTC",
        shortable=raw.get("shortable"),
        fractionable=raw.get("fractionable"),
        last_price=None,  # the asset feed carries no price
        provider="alpaca",
        as_of=datetime.now(UTC),
    )


class AlpacaUniverseProvider:
    """The real production source: Alpaca's `/v2/assets`.

    One request for the whole list — thousands of records — which is why
    reference data is cached for hours rather than fetched per cycle. The feed
    is the only place that knows about new listings, delistings and halts, and
    it is the reason the desk can be pointed at a thousand names without anyone
    maintaining a list of them.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

    @property
    def name(self) -> str:
        return "alpaca"

    def _headers(self) -> dict[str, str]:
        # In headers, never the query string: a key in a URL ends up in an
        # exception message and from there in a log.
        return {
            "APCA-API-KEY-ID": self._settings.alpaca_api_key or "",
            "APCA-API-SECRET-KEY": self._settings.alpaca_api_secret or "",
        }

    async def get_universe(self, *, tier: UniverseTier = UniverseTier.BROAD) -> list[Instrument]:
        if not (self._settings.alpaca_api_key and self._settings.alpaca_api_secret):
            raise RuntimeError("UNIVERSE_PROVIDER_NOT_CONFIGURED:alpaca")

        url = f"{self._settings.alpaca_broker_base_url}/v2/assets"
        params = {"status": "active", "asset_class": "us_equity"}
        async with httpx.AsyncClient(timeout=60.0, trust_env=False) as client:
            resp = await get_with_retry(client, url, params=params, headers=self._headers())
            resp.raise_for_status()
            payload = resp.json()

        if not isinstance(payload, list):
            raise RuntimeError("UNIVERSE_PROVIDER_MALFORMED:alpaca")  # noqa: TRY004

        out: list[Instrument] = []
        for raw in payload:
            if isinstance(raw, dict):
                instrument = _instrument_from_alpaca(raw)
                if instrument is not None:
                    out.append(instrument)
        return out


def create_universe_provider(settings: Settings | None = None) -> UniverseProvider:
    """The provider the desk runs with.

    Falls back to the curated list when Alpaca is not configured, rather than
    failing to start: an unconfigured vendor should narrow what the desk looks
    at, not stop it looking. Nothing downstream is weakened by a smaller
    universe — every gate still runs on whatever does arrive.
    """
    resolved = settings or get_settings()
    if resolved.alpaca_api_key and resolved.alpaca_api_secret:
        return AlpacaUniverseProvider(resolved)
    return StaticUniverseProvider()
