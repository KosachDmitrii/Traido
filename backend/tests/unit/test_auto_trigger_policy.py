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


def test_enqueue_deduplicates_an_opportunity(monkeypatch: pytest.MonkeyPatch) -> None:
    from uuid import uuid4

    atp.set_auto_trigger_enabled(True, actor="test")

    class _Queue:
        def __init__(self) -> None:
            self.items = []

        def put_nowait(self, item) -> None:
            self.items.append(item)

    queue = _Queue()
    monkeypatch.setattr(atp, "_ensure_worker", lambda: queue)
    opp_id = uuid4()
    audit = object()
    assert atp.enqueue_auto_approve_opportunity(opp_id, audit=audit, symbol="AAPL") is True
    assert atp.enqueue_auto_approve_opportunity(opp_id, audit=audit, symbol="AAPL") is False
    assert len(queue.items) == 1


def test_persisted_retry_deadline_survives_memory_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    from uuid import uuid4

    atp.set_auto_trigger_enabled(True, actor="test")
    opp_id = uuid4()
    store = MagicMock()
    store.get.return_value = SimpleNamespace(
        auto_trigger_retry_at=datetime.now(UTC) + timedelta(minutes=1)
    )
    monkeypatch.setattr("trading.opportunities.OPPORTUNITIES", store)
    ensure = MagicMock()
    monkeypatch.setattr(atp, "_ensure_worker", ensure)

    assert atp.enqueue_auto_approve_opportunity(opp_id, audit=object(), symbol="AAPL") is False
    ensure.assert_not_called()


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


@pytest.mark.asyncio
async def test_orb_toggle_routes_and_desk_share_policy() -> None:
    from api.routes.desk import _auto_trigger_payload
    from api.routes.trading import AutoTriggerBody, get_auto_trigger, put_auto_trigger

    assert (await get_auto_trigger())["enabled"] is False
    assert (await put_auto_trigger(AutoTriggerBody(enabled=True)))["enabled"] is True
    assert _auto_trigger_payload()["enabled"] is True
    assert (await put_auto_trigger(AutoTriggerBody(enabled=False)))["enabled"] is False


@pytest.mark.asyncio
async def test_orb_toggle_live_rejected(monkeypatch) -> None:
    from fastapi import HTTPException

    from api.routes.trading import AutoTriggerBody, put_auto_trigger

    monkeypatch.setattr(atp, "_auto_trigger_blocked", lambda: True)
    with pytest.raises(HTTPException) as exc:
        await put_auto_trigger(AutoTriggerBody(enabled=True))
    assert exc.value.status_code == 409
    assert atp.get_auto_trigger_enabled() is False


@pytest.mark.asyncio
async def test_orb_loop_dispatches_after_observation(monkeypatch) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    from core import audit
    from strategy.orb import loop

    events = []
    monkeypatch.setattr(loop.time, "monotonic", lambda: -1)

    async def observe():
        events.append("observe")

    monkeypatch.setattr(loop, "observe", observe)
    monkeypatch.setattr(audit, "create_audit", lambda: object())
    monkeypatch.setattr(
        atp, "enqueue_auto_approve_open_buys", lambda **kwargs: events.append("queue")
    )
    monkeypatch.setattr(loop.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await loop._run()
    assert events == ["observe", "queue"]


@pytest.mark.parametrize("reason", ["ORB_WAITING_BREAKOUT", "ORB_ENTRY_MISSED", "SPREAD_TOO_WIDE"])
def test_price_conditions_are_wait_not_operational_failure(reason):
    assert atp._classify_failure(RuntimeError(f"LIQUIDITY_GATE_REJECTED:{reason}")) == "WAIT"


def test_price_wait_does_not_hide_missing_data_or_unknown_submission():
    assert (
        atp._classify_failure(
            RuntimeError("LIQUIDITY_GATE_REJECTED:ORB_WAITING_BREAKOUT,QUOTE_STALE")
        )
        == "DATA_BLOCKED"
    )
    assert (
        atp._classify_failure(RuntimeError("ENTRY_STATE_UNKNOWN:ORB_WAITING_BREAKOUT")) == "UNKNOWN"
    )


def test_price_wait_retry_does_not_grow_to_five_minutes(monkeypatch):
    from datetime import UTC, datetime
    from uuid import uuid4

    from trading.opportunities import OPPORTUNITIES

    monkeypatch.setattr(OPPORTUNITIES, "get", lambda _: None)
    key = uuid4()
    for _ in range(10):
        before = datetime.now(UTC)
        until = atp._set_retry(
            key,
            operational=False,
            outcome="WAIT",
            error="LIQUIDITY_GATE_REJECTED:ORB_WAITING_BREAKOUT",
        )
        assert 4.9 <= (until - before).total_seconds() <= 5.5


def test_execution_status_projection_preserves_retry_and_tracks_queue_without_writes():
    from datetime import timedelta

    from core.enums import TradingMode
    from strategy.orb.publication import publish_orb
    from tests.conftest import RTH_INSTANT
    from tests.unit.test_orb_publication import proposed
    from trading.opportunities import OpportunityStore

    OPPORTUNITIES = OpportunityStore()

    result, final = proposed()
    opp = publish_orb(result, final, TradingMode.CONFIRMATION, now=RTH_INSTANT)
    opp = OPPORTUNITIES.update(
        opp.model_copy(
            update={
                "auto_trigger_last_outcome": "DATA_BLOCKED",
                "auto_trigger_last_error": "LIQUIDITY_GATE_REJECTED:ORB_QUOTE_STALE",
                "auto_trigger_retry_at": RTH_INSTANT + timedelta(seconds=30),
                "auto_trigger_attempts": 2,
            }
        )
    )
    states = {result.symbol: {"opportunity_id": str(opp.id)}}
    original = OPPORTUNITIES.get(opp.id).model_dump()
    data = atp.orb_execution_statuses(states)[result.symbol]
    assert data["stage"] == "DATA_BLOCKED" and data["attempts"] == 2
    assert data["last_error"].endswith("ORB_QUOTE_STALE")
    atp._queued.add(str(opp.id))
    assert atp.orb_execution_statuses(states)[result.symbol]["stage"] == "QUEUED"
    atp._in_flight.add(str(opp.id))
    assert atp.orb_execution_statuses(states)[result.symbol]["stage"] == "CHECKING"
    assert OPPORTUNITIES.get(opp.id).model_dump() == original


def test_executed_status_wins_over_stale_worker_flags():
    from core.enums import OpportunityStatus, TradingMode
    from strategy.orb.publication import publish_orb
    from tests.conftest import RTH_INSTANT
    from tests.unit.test_orb_publication import proposed
    from trading.opportunities import OpportunityStore

    OPPORTUNITIES = OpportunityStore()

    result, final = proposed()
    opp = publish_orb(result, final, TradingMode.CONFIRMATION, now=RTH_INSTANT)
    OPPORTUNITIES.claim(
        opp.id,
        from_status=OpportunityStatus.AWAITING_CONFIRMATION,
        to_status=OpportunityStatus.EXECUTED,
    )
    atp._in_flight.add(str(opp.id))
    states = {result.symbol: {"opportunity_id": str(opp.id)}}
    assert atp.orb_execution_statuses(states)[result.symbol]["stage"] == "EXECUTED"
