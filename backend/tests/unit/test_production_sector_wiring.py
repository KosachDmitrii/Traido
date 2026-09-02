"""Production wiring: SectorAssessmentPort without permissive monkeypatch."""

from __future__ import annotations

import pytest

from api.deps import build_execution_service
from trading.sector_assessment import MetadataSectorAssessment, get_sector_assessment_port


@pytest.mark.asyncio
async def test_default_sector_port_is_metadata_driven() -> None:
    port = get_sector_assessment_port()
    assert isinstance(port, MetadataSectorAssessment)
    nem = await port.assess("NEM")
    assert nem.benchmark == "GDX"
    assert nem.tradable_long is True
    assert nem.fresh is True
    unknown = await port.assess("ZZZZ")
    assert unknown.tradable_long is None
    assert unknown.fresh is False


def test_build_execution_service_arms_market_data() -> None:
    svc = build_execution_service()
    assert svc.market_data is not None
