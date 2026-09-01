"""Desk light ETag + broker split."""

from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from api.main import app
from api.routes import desk as desk_mod


def test_etag_stable_for_identical_payload() -> None:
    payload = {
        "rev": 1,
        "scanner": {"cycle": 1, "running": False, "last_symbol": None},
        "buy_opportunities": [],
        "sell_opportunities": [],
        "positions": [],
        "review": {"trade_count": 0},
        "activity": {
            "agents": [
                {
                    "id": "scanner",
                    "status": "idle",
                    "detail": "ok",
                    "score": None,
                    "last_symbol": None,
                }
            ]
        },
    }
    assert desk_mod._etag_for(payload) == desk_mod._etag_for(payload)


def test_desk_etag_304() -> None:
    with (
        patch("agents.scanner.agent.start_scanner"),
        patch("agents.scanner.agent.stop_scanner"),
        patch("api.routes.desk.build_review") as br,
    ):
        from agents.review.agent import ReviewReport

        br.return_value = ReviewReport(
            trade_count=0,
            win_count=0,
            loss_count=0,
            win_rate=0.0,
            expectancy=None,
            profit_factor=None,
            avg_pnl=None,
            avg_pnl_pct=None,
            by_strategy=[],
            by_symbol=[],
            recent=[],
            notes=[],
        )
        client = TestClient(app)
        r1 = client.get("/api/v1/desk")
        assert r1.status_code == 200
        etag = r1.headers.get("etag")
        assert etag
        r2 = client.get("/api/v1/desk", headers={"If-None-Match": etag})
        assert r2.status_code == 304, (etag, r2.headers.get("etag"), r2.status_code)


def test_desk_broker_endpoint() -> None:
    with (
        patch("agents.scanner.agent.start_scanner"),
        patch("agents.scanner.agent.stop_scanner"),
    ):
        client = TestClient(app)
        r = client.get("/api/v1/desk/broker")
        assert r.status_code == 200
        body = r.json()
        assert "portfolio" in body
        assert "open_orders" in body
        assert "positions" in body
