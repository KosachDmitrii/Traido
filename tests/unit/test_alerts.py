"""Alert bus — dedupe, cooldown, fail-soft delivery."""

from __future__ import annotations

import pytest

from core.alerts import ALERTS, Alert, AlertSeverity, alert_if


@pytest.fixture(autouse=True)
def _clean_alerts() -> None:
    ALERTS.reset()
    yield
    ALERTS.reset()


@pytest.mark.asyncio
async def test_alerts_dedupe_within_cooldown() -> None:
    a = Alert(key="k1", severity=AlertSeverity.CRITICAL, title="t", body="b")
    assert await ALERTS.emit(a) is True
    assert await ALERTS.emit(a) is False
    assert len(ALERTS.recent()) == 1


@pytest.mark.asyncio
async def test_alert_if_skips_false_condition() -> None:
    assert (
        await alert_if(
            key="k2",
            severity=AlertSeverity.HIGH,
            title="nope",
            condition=False,
        )
        is False
    )
    assert ALERTS.recent() == []
