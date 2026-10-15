"""Owner stop switch: persistence, broker protection, independent target exits."""

from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from core.audit import InMemoryAudit
from core.config import get_settings
from core.enums import BrokerEnvironment, OrderSide, OrderStatus, OrderType
from core.schemas import OrderRecord
from strategy.orb.loop import exit_due_positions
from tests.unit.test_orb_policy import quote
from tests.unit.test_orb_retest import ready_plan
from trading import auto_stop_policy as policy
from trading.exit_policy import PROTECTED_INTRADAY, automatic_stops_enabled
from trading.reconcile import ReconciliationReport, reconcile_protective_orders


def test_no_selection_preserves_new_and_legacy_policies(monkeypatch):
    monkeypatch.setattr(get_settings(), "paper_exit_policy", "manual_target")
    assert policy.get_override() is None
    assert not automatic_stops_enabled({})
    assert automatic_stops_enabled({"position_exit_policy": PROTECTED_INTRADAY})
    assert policy.policy_payload() == {"enabled": True, "configured": False, "available": True}


@pytest.mark.parametrize("enabled", [False, True])
def test_file_selection_survives_restart_and_applies_to_both_policies(monkeypatch, enabled):
    monkeypatch.setattr(get_settings(), "paper_exit_policy", "manual_target")
    policy.set_enabled(enabled)
    policy.reset_cache()
    assert policy.get_override() is enabled
    assert automatic_stops_enabled({}) is enabled
    assert automatic_stops_enabled({"position_exit_policy": PROTECTED_INTRADAY}) is enabled


def test_redis_is_authoritative_and_failed_write_does_not_change_selection(monkeypatch):
    values = {}

    class Redis:
        fail = False

        def get(self, key):
            return values.get(key)

        def set(self, key, raw):
            if self.fail:
                raise TimeoutError("unavailable")
            values[key] = raw
            return True

    client = Redis()
    monkeypatch.setenv("REDIS_URL", "redis://test")
    monkeypatch.setattr(policy, "_redis_client", lambda: client)
    policy.set_enabled(False)
    policy.reset_cache()
    assert policy.get_override() is False
    client.fail = True
    with pytest.raises(TimeoutError):
        policy.set_enabled(True)
    assert policy.get_override() is False
    assert not policy.POLICY_PATH.exists()


@pytest.mark.parametrize("raw", ['{"enabled":"false"}', '{"enabled":0}', "broken"])
def test_corrupt_policy_never_guesses(raw):
    policy.POLICY_PATH.write_text(raw)
    with pytest.raises(ValueError):
        policy.get_override()


def test_live_cannot_disable_protection(monkeypatch):
    monkeypatch.setattr(get_settings(), "broker_env", BrokerEnvironment.LIVE)
    with pytest.raises(ValueError, match="PAPER_ONLY"):
        policy.set_enabled(False)
    assert automatic_stops_enabled({})
    assert not policy.policy_payload()["available"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_confirmed", [False, True])
async def test_off_cancels_owned_stop_and_never_reinstalls(monkeypatch, cancel_confirmed):
    policy.set_enabled(False)
    row = SimpleNamespace(
        id=uuid4(),
        symbol="AAPL",
        qty=D(5),
        stop_price=D(99),
        payload={"position_exit_policy": PROTECTED_INTRADAY, "stop_order_id": "owned"},
    )
    owned = OrderRecord(
        id=uuid4(),
        client_order_id="owned",
        broker_order_id="owned",
        symbol="AAPL",
        side=OrderSide.SELL,
        order_type=OrderType.STOP,
        qty=D(5),
        stop_price=D(99),
        status=OrderStatus.ACCEPTED,
    )
    foreign = owned.model_copy(
        update={"broker_order_id": "external", "client_order_id": "external"}
    )
    broker = SimpleNamespace(list_open_orders=AsyncMock(return_value=[owned, foreign]))
    execution = SimpleNamespace(
        cancel_protection=AsyncMock(return_value=cancel_confirmed), ensure_protection=AsyncMock()
    )
    ledger = SimpleNamespace(get_open=lambda: [row])
    report = ReconciliationReport()
    await reconcile_protective_orders(broker, ledger, execution=execution, report=report)
    execution.cancel_protection.assert_awaited_once_with(
        broker_order_id="owned",
        symbol="AAPL",
        reason="owner_automatic_stop_disabled",
    )
    execution.ensure_protection.assert_not_awaited()
    assert bool(report.unresolved) is not cancel_confirmed


@pytest.mark.asyncio
async def test_on_restores_legacy_stop_from_saved_geometry(monkeypatch):
    monkeypatch.setattr(get_settings(), "paper_exit_policy", "manual_target")
    policy.set_enabled(True)
    row = SimpleNamespace(id=uuid4(), symbol="AAPL", qty=D(5), stop_price=D(99), payload={})
    broker = SimpleNamespace(
        list_open_orders=AsyncMock(return_value=[]), list_positions=AsyncMock(return_value=[row])
    )
    execution = SimpleNamespace(ensure_protection=AsyncMock(return_value="new-stop"))
    ledger = SimpleNamespace(get_open=lambda: [row], set_stop_order_id=Mock())
    assert await reconcile_protective_orders(broker, ledger, execution=execution) == 1
    execution.ensure_protection.assert_awaited_once_with(
        symbol="AAPL",
        qty=D(5),
        stop_price=D(99),
        position_id=row.id,
        reason="reconcile: protective stop missing at broker",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("target_hit", [False, True])
async def test_target_and_existing_time_exits_are_independent(monkeypatch, enabled, target_hit):
    from api import deps
    from trading.ledger import LEDGER

    policy.set_enabled(enabled)
    p, _, opened = ready_plan()
    now = opened + timedelta(minutes=40)
    bid = "102.50" if target_hit else "101.00"
    row = SimpleNamespace(
        id=uuid4(),
        symbol=p.symbol,
        qty=D(5),
        avg_entry=D("101.12"),
        opened_at=opened,
        payload={
            "position_exit_policy": PROTECTED_INTRADAY,
            "exit_policy": "session_close",
            "exit_at": p.exit_at.isoformat(),
            "orb_plan": p.model_dump(mode="json"),
        },
    )
    monkeypatch.setattr(LEDGER, "get_open", lambda: [row])
    execution = SimpleNamespace(
        quotes=SimpleNamespace(
            get_quote=AsyncMock(return_value=quote(bid, str(D(bid) + D(".02")), now))
        ),
        audit=InMemoryAudit(),
        close_position=AsyncMock(),
    )
    monkeypatch.setattr(deps, "build_execution_service", lambda: execution)
    assert await exit_due_positions(now=now) == 1
    execution.close_position.assert_awaited_once_with(
        p.symbol,
        reason="ORB_TARGET_REACHED" if target_hit else "ORB_TIME_NO_PROGRESS",
    )


@pytest.mark.asyncio
async def test_off_suppresses_protected_entry_stop_and_failure_flatten():
    from tests.unit.test_exit_durability import _ExitBroker, _service
    from trading.exits import MemoryExitStore
    from trading.intents import MemoryOrderIntentStore

    policy.set_enabled(False)
    broker = _ExitBroker()
    service = _service(broker, MemoryExitStore(), MemoryOrderIntentStore(), InMemoryAudit())
    assert (
        await service._place_protective_stop(
            symbol="AAPL",
            qty=D(5),
            stop_price=D(99),
            position_id=None,
            reason="test",
            protected_entry=True,
        )
        is None
    )
    assert not await service._emergency_flatten(
        symbol="AAPL",
        qty=D(5),
        pipeline_run_id=None,
        reason="test",
        protected_entry=True,
    )
    assert broker.orders == [] and broker.market_sells == []


@pytest.mark.asyncio
async def test_api_saves_policy_and_requests_broker_reconciliation(monkeypatch):
    from api.routes.trading import AutoStopBody, put_auto_stop
    from trading.reconcile_supervisor import RECONCILE

    run = AsyncMock(return_value=SimpleNamespace(ok=True, unresolved=()))
    monkeypatch.setattr(RECONCILE, "run", run)
    result = await put_auto_stop(AutoStopBody(enabled=False))
    assert result["enabled"] is False and result["reconciliation_ok"] is False
    run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("unresolved", [[], ["exit_policy:AAPL:stop_cancel_unconfirmed"]])
async def test_api_only_confirms_a_pass_that_applied_the_selection(monkeypatch, unresolved):
    from api import deps
    from api.routes.trading import AutoStopBody, put_auto_stop
    from trading.reconcile_supervisor import RECONCILE

    async def run(pass_fn):
        await pass_fn()

    monkeypatch.setattr(RECONCILE, "run", run)
    monkeypatch.setattr(
        deps, "build_reconcile_pass", AsyncMock(return_value={"unresolved": unresolved})
    )
    result = await put_auto_stop(AutoStopBody(enabled=False))
    assert result["reconciliation_ok"] is (not unresolved)
