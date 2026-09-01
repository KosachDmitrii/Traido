"""
IBKR instrument identity.

The failure being prevented: sending an order for "AAPL" and having IB route it
to a Mexican listing, a European line, or an OTC shell. The resolver's job is
to produce exactly one conId or to refuse.
"""

from __future__ import annotations

from typing import Any

import pytest

from broker.ibkr.config import IBKRConfigError, IBKRTransportConfig
from broker.ibkr.instruments import (
    AmbiguousInstrument,
    IBKRInstrumentResolver,
    InstrumentNotFound,
    UnsupportedInstrument,
)
from core.enums import BrokerEnvironment


def _stk(**overrides: Any) -> dict[str, Any]:
    base = {
        "symbol": "AAPL",
        "conId": 265598,
        "secType": "STK",
        "exchange": "SMART",
        "primaryExchange": "NASDAQ",
        "currency": "USD",
    }
    return {**base, **overrides}


class _Source:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls = 0

    async def resolve_contract(self, symbol: str) -> list[dict[str, Any]]:
        self.calls += 1
        return self.rows


async def test_a_unique_us_listing_resolves_to_its_con_id() -> None:
    resolver = IBKRInstrumentResolver(_Source([_stk()]))

    instrument = await resolver.resolve("aapl")

    assert instrument.con_id == 265598
    assert instrument.symbol == "AAPL"
    assert instrument.currency == "USD"
    assert instrument.primary_exchange == "NASDAQ"


async def test_an_ambiguous_symbol_is_refused_not_guessed() -> None:
    source = _Source([_stk(conId=265598), _stk(conId=999999, primaryExchange="ARCA")])
    resolver = IBKRInstrumentResolver(source)

    with pytest.raises(AmbiguousInstrument, match="2 IB contracts"):
        await resolver.resolve("AAPL")


async def test_no_contract_found_is_an_error_not_an_empty_order() -> None:
    with pytest.raises(InstrumentNotFound):
        await IBKRInstrumentResolver(_Source([])).resolve("NOSUCH")


async def test_a_non_usd_listing_is_rejected() -> None:
    resolver = IBKRInstrumentResolver(_Source([_stk(currency="EUR", primaryExchange="IBIS")]))

    with pytest.raises(UnsupportedInstrument, match="not USD"):
        await resolver.resolve("AAPL")


@pytest.mark.parametrize("sec_type", ["OPT", "FUT", "CASH", "CRYPTO"])
async def test_unsupported_instrument_types_are_rejected(sec_type: str) -> None:
    resolver = IBKRInstrumentResolver(_Source([_stk(secType=sec_type)]))

    with pytest.raises(UnsupportedInstrument, match="unsupported secType"):
        await resolver.resolve("AAPL")


async def test_otc_venues_are_blocked_at_the_identity_layer() -> None:
    resolver = IBKRInstrumentResolver(_Source([_stk(primaryExchange="PINK")]))

    with pytest.raises(UnsupportedInstrument, match="OTC"):
        await resolver.resolve("AAPL")


async def test_a_resolved_con_id_is_cached() -> None:
    source = _Source([_stk()])
    resolver = IBKRInstrumentResolver(source)

    first = await resolver.resolve("AAPL")
    second = await resolver.resolve("AAPL")

    assert first == second
    assert source.calls == 1, "a conId is a stable fact; re-asking IB is waste"
    assert "AAPL" in resolver.cached_symbols


async def test_a_failure_is_not_cached() -> None:
    """A missing contract is usually a connection problem, so it must be retried."""
    source = _Source([])
    resolver = IBKRInstrumentResolver(source)

    with pytest.raises(InstrumentNotFound):
        await resolver.resolve("AAPL")
    source.rows = [_stk()]

    assert (await resolver.resolve("AAPL")).con_id == 265598


# ── Paper / live separation ──────────────────────────────────────────────────
#
# Synchronous by nature: configuration is validated before anything connects.


def test_a_paper_environment_cannot_point_at_a_live_port() -> None:
    """The mistake this exists to make impossible."""
    with pytest.raises(IBKRConfigError, match="live IB port"):
        IBKRTransportConfig(port=7496, environment=BrokerEnvironment.PAPER)


def test_a_live_environment_cannot_point_at_a_paper_port() -> None:
    with pytest.raises(IBKRConfigError, match="paper IB port"):
        IBKRTransportConfig(port=7497, environment=BrokerEnvironment.LIVE)


def test_the_default_configuration_is_paper() -> None:
    config = IBKRTransportConfig()

    assert config.is_paper
    assert config.port in {7497, 4002}


def test_configuration_read_from_an_empty_environment_is_paper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in ("TRAIDO_IBKR_ENV", "TRAIDO_IBKR_PORT", "TRAIDO_IBKR_HOST"):
        monkeypatch.delenv(key, raising=False)

    assert IBKRTransportConfig.from_env().is_paper


def test_the_startup_line_names_the_environment_and_leaks_nothing() -> None:
    line = IBKRTransportConfig(account="DU1234567").describe()

    assert "BROKER=IBKR" in line
    assert "ENVIRONMENT=PAPER" in line
    for secret in ("password", "token", "secret", "api_key"):
        assert secret not in line.lower()
