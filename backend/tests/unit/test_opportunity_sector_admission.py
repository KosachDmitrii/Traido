"""Creation admission must not publish a BUY that approval cannot authorize."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from core.config import Settings
from core.enums import (
    AdmissionDecision,
    DataHealthStatus,
    RiskVerdict,
    TradeAction,
)
from core.schemas import PipelineResult, PortfolioSnapshot, RiskDecision, TradeCandidate
from risk.limits import default_risk_limits
from trading.pipeline import publish_opportunity
from trading.sector_assessment import SectorMarketAssessment, set_sector_assessment_port


def _candidate() -> TradeCandidate:
    return TradeCandidate(
        symbol="CNQ",
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal("51.37"),
        stop=Decimal("49.59"),
        target=Decimal("54.93"),
        risk_reward=2.0,
        reasons=["test"],
        strategy_version="test@1",
    )


def _risk() -> RiskDecision:
    return RiskDecision(
        verdict=RiskVerdict.PASS,
        sized_qty=Decimal(10),
        limits_applied=default_risk_limits(),
        portfolio=PortfolioSnapshot(
            equity=Decimal(100000),
            cash=Decimal(100000),
            buying_power=Decimal(100000),
            open_exposure=Decimal(0),
            open_positions=0,
            day_pnl=Decimal(0),
            week_pnl=Decimal(0),
            drawdown_pct=0,
        ),
    )


@pytest.mark.asyncio
async def test_missing_sector_never_creates_actionable_opportunity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BlockedSector:
        async def assess(self, symbol: str, **_kwargs):
            return SectorMarketAssessment(
                symbol=symbol,
                evaluated_at=datetime.now(UTC),
                data_status=DataHealthStatus.UNHEALTHY,
                tradable_long=None,
                reason_codes=("SECTOR_METADATA_MISSING", "SECTOR_ASSESSMENT_MISSING"),
            )

    created: list[str] = []
    monkeypatch.setattr("trading.pipeline.OPPORTUNITIES.list_open", list)
    monkeypatch.setattr(
        "trading.pipeline.OPPORTUNITIES.create",
        lambda *_args, **_kwargs: created.append("created"),
    )
    monkeypatch.setattr("trading.pipeline.DECISION_OUTCOMES.record", lambda **_kwargs: None)
    monkeypatch.setattr("trading.pipeline.BOARD.set_agent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("trading.pipeline.BOARD.log", lambda *_args, **_kwargs: None)
    set_sector_assessment_port(_BlockedSector())
    try:
        candidate = _candidate()
        risk = _risk()
        result = await publish_opportunity(
            PipelineResult(
                pipeline_run_id=uuid4(),
                symbol="CNQ",
                status="risk_passed",
                candidate=candidate,
                risk=risk,
            ),
            risk,
            settings=Settings(),
            admission=SimpleNamespace(
                decision=AdmissionDecision.BUY_ALLOWED,
                admitted=True,
                data_status=DataHealthStatus.HEALTHY,
            ),  # type: ignore[arg-type]
            market_data=SimpleNamespace(),  # type: ignore[arg-type]
        )
    finally:
        set_sector_assessment_port(None)

    assert result.status == "data_blocked"
    assert result.opportunity is None
    assert result.errors == ["SECTOR_METADATA_MISSING", "SECTOR_ASSESSMENT_MISSING"]
    assert created == []
