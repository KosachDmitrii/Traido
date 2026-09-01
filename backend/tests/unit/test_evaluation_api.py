"""Evaluation endpoint behaviour, especially when the data vendor is down.

The evaluation surface is read-only, but it is the page a trader looks at
before sizing real money. A vendor outage has to look like a vendor outage,
not like a strategy with no edge and not like a 500.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core.enums import Timeframe
from core.schemas import Bar
from quant.backtesting.service import MarketDataUnavailable, evaluate_symbol


class _DownProvider:
    async def get_bars(self, symbol, timeframe, start, end):  # type: ignore[no-untyped-def]
        raise ConnectionError("vendor unreachable")


class _BenchmarkOnlyDownProvider:
    """Symbol history works; only the benchmark fetch fails."""

    def __init__(self, bars: list[Bar]) -> None:
        self._bars = bars

    async def get_bars(self, symbol, timeframe, start, end):  # type: ignore[no-untyped-def]
        if symbol == "SPY":
            raise ConnectionError("vendor unreachable")
        return self._bars


def _bars(symbol: str = "AAPL", n: int = 400) -> list[Bar]:
    out: list[Bar] = []
    price = 100.0
    for i in range(n):
        price *= 1.001
        close = Decimal(str(round(price, 2)))
        out.append(
            Bar(
                symbol=symbol,
                timeframe=Timeframe.D1,
                ts=datetime(2020, 1, 1, tzinfo=UTC) + timedelta(days=i),
                open=close,
                high=close * Decimal("1.01"),
                low=close * Decimal("0.99"),
                close=close,
                volume=1_000_000,
                source="synthetic",
            )
        )
    return out


@pytest.mark.asyncio
async def test_vendor_outage_raises_a_typed_error_not_a_bare_exception() -> None:
    with pytest.raises(MarketDataUnavailable):
        await evaluate_symbol("AAPL", market_data=_DownProvider(), use_cache=False)


@pytest.mark.asyncio
async def test_a_missing_benchmark_degrades_instead_of_failing() -> None:
    """Losing SPY costs us the comparison, not the whole evaluation."""
    result = await evaluate_symbol(
        "AAPL",
        market_data=_BenchmarkOnlyDownProvider(_bars()),
        use_cache=False,
    )
    assert result.symbol == "AAPL"


def test_endpoint_reports_a_vendor_outage_as_503(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from api.routes import evaluation as mod

    async def _boom(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise MarketDataUnavailable("could not load history for AAPL")

    monkeypatch.setattr(mod, "evaluate_symbol", _boom)
    with TestClient(app) as client:
        resp = client.get("/api/v1/evaluation/AAPL")

    assert resp.status_code == 503
    assert "could not load history" in resp.json()["detail"]


def test_endpoint_reports_bad_input_as_422(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from api.routes import evaluation as mod

    async def _bad(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise ValueError("not enough history")

    monkeypatch.setattr(mod, "evaluate_symbol", _bad)
    with TestClient(app) as client:
        resp = client.get("/api/v1/evaluation/AAPL")

    assert resp.status_code == 422


def test_batch_endpoint_isolates_per_symbol_failures(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """One dead symbol must not blank the whole batch."""
    from api.routes import evaluation as mod

    async def _half(symbol, **_kwargs):  # type: ignore[no-untyped-def]
        if symbol == "MSFT":
            raise MarketDataUnavailable("down")

        class _R:
            def as_dict(self) -> dict:
                return {"symbol": symbol}

        return _R()

    monkeypatch.setattr(mod, "evaluate_symbol", _half)
    with TestClient(app) as client:
        resp = client.get("/api/v1/evaluation?symbols=AAPL,MSFT")

    assert resp.status_code == 200
    body = resp.json()
    assert [r["symbol"] for r in body["results"]] == ["AAPL"]
    assert "MSFT" in body["errors"]
