"""Sector assessment port — fail-closed without metadata."""

from __future__ import annotations

import pytest

from trading.sector_assessment import MetadataSectorAssessment


@pytest.mark.asyncio
async def test_known_symbol_tradable() -> None:
    port = MetadataSectorAssessment()
    result = await port.assess("AAPL")
    assert result.fresh is True
    assert result.tradable_long is True
    assert result.sector_label == "technology"
    assert result.benchmark == "XLK"


@pytest.mark.asyncio
async def test_unknown_symbol_blocked() -> None:
    port = MetadataSectorAssessment()
    result = await port.assess("ZZZZ")
    assert result.fresh is False
    assert result.tradable_long is None
    assert "SECTOR_METADATA_MISSING" in result.reason_codes


@pytest.mark.asyncio
async def test_gold_miner_uses_gdx_not_hardcoded_nem_only() -> None:
    port = MetadataSectorAssessment()
    nem = await port.assess("NEM")
    aem = await port.assess("AEM")
    assert nem.benchmark == aem.benchmark == "GDX"
    assert nem.industry_label == "gold_miners"
