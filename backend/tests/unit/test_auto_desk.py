"""Auto scanner + desk confirmation UX."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_desk_returns_proposal_queues(monkeypatch, tmp_path) -> None:
    # Faster tiny universe for test
    watch = {
        "universe": ["AAPL"],
        "timeframes": ["1d"],
        "scan_interval_seconds": 600,
        "max_open_buy_opportunities": 5,
        "enabled": True,
    }
    path = Path("configs/watchlist.json")
    original = path.read_text()
    path.write_text(json.dumps(watch))
    try:
        import os

        os.environ["TRAIDO_BROKER_MOCK"] = "true"
        os.environ["TRAIDO_AUTH_DISABLED"] = "true"
        from api.main import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test", timeout=60.0) as client:
            run = await client.post("/api/v1/scanner/run")
            assert run.status_code == 200
            desk = await client.get("/api/v1/desk")
            assert desk.status_code == 200
            body = desk.json()
            assert "buy_opportunities" in body
            assert "sell_opportunities" in body
            assert "review" in body
            assert "positions" in body
            assert "activity" in body
            assert "You only confirm" in body["message"] or "confirm" in body["message"].lower()
            assert "universe" in body["scanner"]
    finally:
        path.write_text(original)
        import os

        os.environ.pop("TRAIDO_BROKER_MOCK", None)
        os.environ.pop("TRAIDO_AUTH_DISABLED", None)
