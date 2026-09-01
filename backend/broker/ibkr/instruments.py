"""
Instrument identity for IBKR.

A ticker is not an instrument. "AAPL" alone matches a US common stock, several
European listings, and any number of derivatives, and IB will happily route to
whichever one it decides you meant. The only unambiguous handle is `conId`, so
nothing reaches the order path until a symbol has been resolved to exactly one.

The resolver refuses rather than picks. If two contracts survive filtering, that
is a fact about the request, not a tie to be broken by ordering.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol

logger = logging.getLogger(__name__)

SUPPORTED_SEC_TYPES: frozenset[str] = frozenset({"STK"})
"""US stocks and ETFs only in V1. Options, futures and FX are out of scope."""

SUPPORTED_CURRENCIES: frozenset[str] = frozenset({"USD"})

BLOCKED_EXCHANGES: frozenset[str] = frozenset(
    {"PINK", "OTCBB", "OTC", "OTCMKTS", "EXPM", "VALUE", "GREY"}
)
"""OTC venues. Blocked by policy, and cheap to block here at the identity layer."""


@dataclass(frozen=True)
class Instrument:
    """One unambiguous tradable contract."""

    symbol: str
    con_id: int
    sec_type: str
    exchange: str
    primary_exchange: str | None
    currency: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "conId": self.con_id,
            "secType": self.sec_type,
            "exchange": self.exchange,
            "primaryExchange": self.primary_exchange,
            "currency": self.currency,
        }


class InstrumentError(RuntimeError):
    """Base for identity failures. All of them block the order."""


class InstrumentNotFound(InstrumentError):
    pass


class AmbiguousInstrument(InstrumentError):
    """More than one contract matched. Never resolved by guessing."""


class UnsupportedInstrument(InstrumentError):
    """Matched something real that V1 is not allowed to trade."""


class InstrumentResolver(Protocol):
    async def resolve(self, symbol: str) -> Instrument: ...


class ContractSource(Protocol):
    """Raw contract lookup, in IB vocabulary."""

    async def resolve_contract(self, symbol: str) -> list[dict[str, Any]]: ...


class IBKRInstrumentResolver:
    """Resolve a ticker to one IB contract, and remember the answer.

    Caching is by symbol and only ever holds successful resolutions: a conId is
    a stable fact about a listing, whereas a failure is usually a fact about the
    connection and must be retried.
    """

    def __init__(self, source: ContractSource) -> None:
        self._source = source
        self._lock = Lock()
        self._cache: dict[str, Instrument] = {}

    @property
    def cached_symbols(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._cache)

    async def resolve(self, symbol: str) -> Instrument:
        ticker = symbol.upper().strip()
        if not ticker:
            raise InstrumentNotFound("empty symbol")

        with self._lock:
            hit = self._cache.get(ticker)
        if hit is not None:
            return hit

        raw = await self._source.resolve_contract(ticker)
        instrument = self._choose(ticker, raw)
        with self._lock:
            self._cache[ticker] = instrument
        return instrument

    @staticmethod
    def _choose(ticker: str, rows: list[dict[str, Any]]) -> Instrument:
        if not rows:
            raise InstrumentNotFound(f"no IB contract for {ticker}")

        # Report the most specific reason available. "Wrong currency" and "no
        # such symbol" are different problems and lead to different fixes.
        matching_symbol = [r for r in rows if str(r.get("symbol", "")).upper() == ticker]
        pool = matching_symbol or rows

        supported_type = [r for r in pool if str(r.get("secType", "")) in SUPPORTED_SEC_TYPES]
        if not supported_type:
            kinds = sorted({str(r.get("secType")) for r in pool})
            raise UnsupportedInstrument(f"{ticker} resolves to unsupported secType {kinds}")

        right_currency = [
            r for r in supported_type if str(r.get("currency", "")).upper() in SUPPORTED_CURRENCIES
        ]
        if not right_currency:
            found = sorted({str(r.get("currency")) for r in supported_type})
            raise UnsupportedInstrument(f"{ticker} is not USD-denominated (got {found})")

        tradable = [r for r in right_currency if not _is_otc(r)]
        if not tradable:
            raise UnsupportedInstrument(f"{ticker} trades OTC, which V1 does not allow")

        distinct = {int(r["conId"]) for r in tradable if r.get("conId")}
        if len(distinct) > 1:
            raise AmbiguousInstrument(
                f"{ticker} matches {len(distinct)} IB contracts {sorted(distinct)}; "
                "resolve it explicitly rather than letting IB choose"
            )
        if not distinct:
            raise InstrumentNotFound(f"IB returned contracts for {ticker} without a conId")

        chosen = next(r for r in tradable if r.get("conId"))
        return Instrument(
            symbol=ticker,
            con_id=int(chosen["conId"]),
            sec_type=str(chosen["secType"]),
            # SMART is IB's router, not a venue. The primary exchange is what
            # disambiguates the listing when two venues carry the same ticker.
            exchange=str(chosen.get("exchange") or "SMART"),
            primary_exchange=_primary(chosen),
            currency=str(chosen["currency"]).upper(),
        )


def _primary(row: dict[str, Any]) -> str | None:
    value = row.get("primaryExchange") or row.get("primary_exchange")
    return str(value) if value else None


def _is_otc(row: dict[str, Any]) -> bool:
    venues = {
        str(row.get("primaryExchange") or "").upper(),
        str(row.get("exchange") or "").upper(),
    }
    return bool(venues & BLOCKED_EXCHANGES)
