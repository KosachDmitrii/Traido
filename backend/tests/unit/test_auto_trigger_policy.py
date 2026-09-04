"""Auto-trigger toggle — persisted operator setting."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trading import auto_trigger_policy as atp


@pytest.fixture(autouse=True)
def _isolate_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "auto_trigger.json"
    store: dict[str, dict[str, str]] = {}

    class _FakeRedis:
        def hget(self, key, field):
            raw = store.get(key, {}).get(field)
            return raw.encode() if raw is not None else None

        def hset(self, key, mapping):
            store[key] = {str(k): str(v) for k, v in mapping.items()}
            return True

        def ping(self):
            return True

    monkeypatch.setattr(atp, "POLICY_PATH", path)
    monkeypatch.setattr(atp, "_redis_client", lambda: _FakeRedis())
    monkeypatch.setattr(atp, "_auto_trigger_blocked", lambda: False)
    atp.reset_auto_trigger_cache()
    yield
    atp.reset_auto_trigger_cache()


def test_default_off() -> None:
    assert atp.get_auto_trigger_enabled() is False
    payload = atp.policy_payload()
    assert payload["enabled"] is False
    assert payload["available"] is True
    assert "note" in payload


def test_set_persists_to_file_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "auto_trigger.json"
    monkeypatch.setattr(atp, "POLICY_PATH", path)
    assert atp.set_auto_trigger_enabled(False, actor="test") is False
    assert atp.get_auto_trigger_enabled() is False
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["enabled"] is False
    assert raw["actor"] == "test"


def test_can_enable_on_paper_confirmation_desk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "auto_trigger.json"
    monkeypatch.setattr(atp, "POLICY_PATH", path)
    assert atp.set_auto_trigger_enabled(True, actor="test") is True
    assert atp.get_auto_trigger_enabled() is True
    payload = atp.policy_payload()
    assert payload["enabled"] is True
    assert payload["available"] is True


def test_cannot_enable_on_live_broker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "auto_trigger.json"
    monkeypatch.setattr(atp, "POLICY_PATH", path)
    monkeypatch.setattr(atp, "_auto_trigger_blocked", lambda: True)
    atp.reset_auto_trigger_cache()
    assert atp.set_auto_trigger_enabled(True, actor="test") is False
    assert atp.get_auto_trigger_enabled() is False
    payload = atp.policy_payload()
    assert payload["enabled"] is False
    assert payload["available"] is False
    assert "live" in payload["note"].lower()


def test_user_file_beats_test_redis_even_when_redis_is_newer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "auto_trigger.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "actor": "user",
                "updated_at": "2026-09-03T15:56:52+00:00",
            }
        ),
        encoding="utf-8",
    )
    store: dict[str, dict[str, str]] = {
        atp.REDIS_KEY: {
            "enabled": "0",
            "actor": "test",
            "updated_at": "2026-09-03T16:10:37+00:00",
        }
    }

    class _FakeRedis:
        def hget(self, key, field):
            raw = store.get(key, {}).get(field)
            return raw.encode() if raw is not None else None

        def hset(self, key, mapping):
            store[key] = {str(k): str(v) for k, v in mapping.items()}
            return True

        def ping(self):
            return True

    monkeypatch.setattr(atp, "POLICY_PATH", path)
    monkeypatch.setattr(atp, "_redis_client", lambda: _FakeRedis())
    atp.reset_auto_trigger_cache()
    assert atp.get_auto_trigger_enabled() is True


@pytest.mark.asyncio
async def test_auto_approve_terminal_regime_discards_card(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from core.audit import InMemoryAudit
    from core.enums import OpportunityStatus

    atp.set_auto_trigger_enabled(True, actor="test")
    opp_id = uuid4()
    opp = MagicMock()
    opp.id = opp_id
    opp.status = OpportunityStatus.AWAITING_CONFIRMATION
    opp.decision_version = 0
    discarded = MagicMock()
    discarded.id = opp_id
    discarded.status = OpportunityStatus.DISCARDED

    store = MagicMock()
    store.get.return_value = opp
    store.claim.return_value = discarded
    audit = InMemoryAudit()
    service = MagicMock()
    service.decide = AsyncMock(side_effect=RuntimeError("BUY_REJECTED_REGIME:REGIME_BLOCKED"))
    monkeypatch.setattr("trading.opportunities.OPPORTUNITIES", store)
    monkeypatch.setattr("api.deps.build_execution_service", lambda: service)

    ok = await atp.maybe_auto_approve_opportunity(opp_id, audit=audit, symbol="AAPL")
    assert ok is False
    store.claim.assert_called_once()
    types = [e["event_type"] for e in audit.events]
    assert "AutoTriggerApproveFailed" in types
    assert "OpportunityDiscarded" in types
    failed = next(e for e in audit.events if e["event_type"] == "AutoTriggerApproveFailed")
    assert failed["payload"]["error"] == "BUY_REJECTED_REGIME:REGIME_BLOCKED"
    store.claim.assert_called_once()
    assert store.claim.call_args.kwargs["to_status"] is OpportunityStatus.DISCARDED


@pytest.mark.asyncio
async def test_auto_approve_wide_spread_keeps_card(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from core.audit import InMemoryAudit
    from core.enums import OpportunityStatus

    atp.set_auto_trigger_enabled(True, actor="test")
    opp_id = uuid4()
    opp = MagicMock()
    opp.id = opp_id
    opp.status = OpportunityStatus.AWAITING_CONFIRMATION
    opp.decision_version = 0
    store = MagicMock()
    store.get.return_value = opp
    audit = InMemoryAudit()
    service = MagicMock()
    service.decide = AsyncMock(side_effect=RuntimeError("BUY_REJECTED_SPREAD:spread_bps=18.4"))
    monkeypatch.setattr("trading.opportunities.OPPORTUNITIES", store)
    monkeypatch.setattr("api.deps.build_execution_service", lambda: service)

    ok = await atp.maybe_auto_approve_opportunity(opp_id, audit=audit, symbol="AAPL")
    assert ok is False
    store.claim.assert_not_called()
    types = [e["event_type"] for e in audit.events]
    assert "AutoTriggerApproveDeferred" in types
    assert "OpportunityDiscarded" not in types


@pytest.mark.asyncio
async def test_auto_approve_stale_quote_keeps_card(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from core.audit import InMemoryAudit
    from core.enums import OpportunityStatus
    from trading.approval_errors import DataBlockedError

    atp.set_auto_trigger_enabled(True, actor="test")
    opp_id = uuid4()
    opp = MagicMock()
    opp.id = opp_id
    opp.status = OpportunityStatus.AWAITING_CONFIRMATION
    opp.decision_version = 0
    store = MagicMock()
    store.get.return_value = opp
    audit = InMemoryAudit()
    service = MagicMock()
    service.decide = AsyncMock(side_effect=DataBlockedError("PORTFOLIO_STATE_UNAVAILABLE"))
    monkeypatch.setattr("trading.opportunities.OPPORTUNITIES", store)
    monkeypatch.setattr("api.deps.build_execution_service", lambda: service)

    ok = await atp.maybe_auto_approve_opportunity(opp_id, audit=audit, symbol="AAPL")
    assert ok is False
    store.claim.assert_not_called()
    types = [e["event_type"] for e in audit.events]
    assert "AutoTriggerApproveDeferred" in types
    assert "OpportunityDiscarded" not in types


@pytest.mark.asyncio
async def test_auto_approve_unknown_after_submit_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from core.audit import InMemoryAudit
    from core.enums import OpportunityStatus

    atp.set_auto_trigger_enabled(True, actor="test")
    opp_id = uuid4()
    opp = MagicMock()
    opp.id = opp_id
    opp.status = OpportunityStatus.AWAITING_CONFIRMATION
    opp.decision_version = 0
    store = MagicMock()
    store.get.return_value = opp
    audit = InMemoryAudit()
    service = MagicMock()
    service.decide = AsyncMock(side_effect=RuntimeError("ENTRY_STATE_UNKNOWN:timeout"))
    monkeypatch.setattr("trading.opportunities.OPPORTUNITIES", store)
    monkeypatch.setattr("api.deps.build_execution_service", lambda: service)

    ok = await atp.maybe_auto_approve_opportunity(opp_id, audit=audit, symbol="AAPL")
    assert ok is False
    store.claim.assert_not_called()
    types = [e["event_type"] for e in audit.events]
    assert "AutoTriggerStateUnknown" in types


@pytest.mark.asyncio
async def test_auto_approve_off_does_not_touch_card(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock
    from uuid import uuid4

    from core.audit import InMemoryAudit

    atp.set_auto_trigger_enabled(False, actor="test")
    store = MagicMock()
    monkeypatch.setattr("trading.opportunities.OPPORTUNITIES", store)

    ok = await atp.maybe_auto_approve_opportunity(uuid4(), audit=InMemoryAudit(), symbol="AAPL")
    assert ok is False
    store.get.assert_not_called()
    store.claim.assert_not_called()
