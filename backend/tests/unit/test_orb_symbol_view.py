"""Symbol inspection reads saved evidence without running admission or publishing."""

import pytest

from api.routes.evaluation import orb_symbol


@pytest.mark.asyncio
async def test_rejected_symbol_survives_missing_quote(monkeypatch):
    import api.routes.evaluation as route
    from strategy.orb import store

    monkeypatch.setattr(
        store,
        "read_session",
        lambda day: {
            "session": day,
            "rejections": {"AAPL": ["ORB_ATR_LOW"]},
            "plans": {},
            "states": {},
        },
    )

    class MissingQuote:
        async def get_quote(self, symbol):
            assert symbol == "AAPL"

    monkeypatch.setattr(route, "create_market_data_port", lambda settings: MissingQuote())
    result = await orb_symbol("aapl")
    assert result["rejections"] == ["ORB_ATR_LOW"]
    assert result["plan"] is None
    assert result["quote"] is None
    assert result["quote_error"] == "ORB_QUOTE_MISSING"


@pytest.mark.asyncio
async def test_plan_hides_evidence_and_vendor_error(monkeypatch):
    import api.routes.evaluation as route
    from strategy.orb import store

    monkeypatch.setattr(
        store,
        "read_session",
        lambda day: {
            "session": day,
            "plans": {"AAPL": {"trigger": "100", "evidence": {"large": "blob"}}},
            "states": {"AAPL": {"state": "WAIT"}},
        },
    )

    class FailedQuote:
        async def get_quote(self, symbol):
            raise RuntimeError("vendor detail must not escape")

    monkeypatch.setattr(route, "create_market_data_port", lambda settings: FailedQuote())
    result = await orb_symbol("AAPL")
    assert result["plan"] == {"trigger": "100"}
    assert result["state"] == {"state": "WAIT"}
    assert result["quote_error"] == "ORB_SERVICE_UNAVAILABLE"
