"""New protected entries and legacy manual holdings may coexist after restart."""

from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from core.config import get_settings
from core.enums import OrderSide, OrderStatus, OrderType
from core.schemas import OrderRecord
from strategy.orb import evaluate_trigger
from strategy.orb.loop import exit_due_positions
from tests.unit.test_orb_retest import ready_plan
from trading.exit_policy import PROTECTED_INTRADAY, manual_target_exits
from trading.reconcile import ReconciliationReport, reconcile_protective_orders


def test_persisted_policy_overrides_global_manual_setting(monkeypatch):
    monkeypatch.setattr(get_settings(), "paper_exit_policy", "manual_target")
    assert manual_target_exits({})
    assert manual_target_exits({"position_exit_policy": None})
    assert not manual_target_exits({"position_exit_policy": PROTECTED_INTRADAY})


@pytest.mark.parametrize("seconds,allowed", [(1, True), (0, False), (-1, False)])
def test_entry_has_full_existing_no_progress_horizon(seconds, allowed):
    from tests.unit.test_orb_policy import quote

    p, _, _ = ready_plan()
    # Persisted pre-rollout plans had the old later deadline. They must also
    # obey the new pre-submit horizon without waiting for rediscovery.
    p = p.model_copy(update={"entry_deadline": p.exit_at - timedelta(minutes=5)})
    now = p.exit_at - timedelta(minutes=30, seconds=seconds)
    result = evaluate_trigger(p, quote("101.12", "101.14", now), now=now)
    if allowed:
        assert "ORB_INSUFFICIENT_SESSION_TIME" not in result.reasons
    else:
        assert result.state == "NO_TRADE"
        assert result.reasons == ["ORB_INSUFFICIENT_SESSION_TIME"]


@pytest.mark.parametrize("day", ["2026-10-14", "2026-11-27"])
def test_new_plan_deadline_tracks_regular_and_early_close(day):
    from datetime import datetime

    from core.clock import ET
    from strategy.orb import form_plan
    from tests.unit.test_orb_policy import evidence

    now = datetime.fromisoformat(f"{day}T10:00:00").replace(tzinfo=ET)
    daily, opening = evidence(now)
    p = form_plan("AAPL", daily, opening, now=now, feed="sip").plan
    assert p is not None
    assert p.exit_at - p.entry_deadline == timedelta(minutes=30)


@pytest.mark.asyncio
async def test_new_position_exits_at_session_end_but_legacy_is_held(monkeypatch):
    from api import deps
    from trading.ledger import LEDGER

    monkeypatch.setattr(get_settings(), "paper_exit_policy", "manual_target")
    p, _, opened = ready_plan()
    rows = [
        SimpleNamespace(
            id=uuid4(),
            symbol=symbol,
            qty=D(5),
            avg_entry=D("101.12"),
            opened_at=opened,
            payload={
                "exit_policy": "session_close",
                "exit_at": p.exit_at.isoformat(),
                **({"position_exit_policy": PROTECTED_INTRADAY} if protected else {}),
            },
        )
        for symbol, protected in [("AAPL", True), ("BWA", False)]
    ]
    monkeypatch.setattr(LEDGER, "get_open", lambda: rows)
    execution = SimpleNamespace(close_position=AsyncMock())
    monkeypatch.setattr(deps, "build_execution_service", lambda: execution)
    assert await exit_due_positions(now=p.exit_at) == 1
    execution.close_position.assert_awaited_once_with("AAPL", reason="ORB_SESSION_END")


@pytest.mark.asyncio
async def test_reconciliation_preserves_new_stop_and_cancels_only_legacy_stop(monkeypatch):
    monkeypatch.setattr(get_settings(), "paper_exit_policy", "manual_target")
    rows = [
        SimpleNamespace(
            id=uuid4(),
            symbol=symbol,
            qty=D(5),
            stop_price=D(99),
            payload={
                "stop_order_id": symbol,
                **({"position_exit_policy": PROTECTED_INTRADAY} if protected else {}),
            },
        )
        for symbol, protected in [("AAPL", True), ("BWA", False)]
    ]
    stops = [
        OrderRecord(
            id=uuid4(),
            client_order_id=symbol,
            symbol=symbol,
            side=OrderSide.SELL,
            order_type=OrderType.STOP,
            qty=D(5),
            stop_price=D(99),
            broker_order_id=symbol,
            status=OrderStatus.SUBMITTED,
        )
        for symbol in ["AAPL", "BWA"]
    ]
    broker = SimpleNamespace(
        list_open_orders=AsyncMock(return_value=stops), list_positions=AsyncMock(return_value=rows)
    )
    execution = SimpleNamespace(
        cancel_protection=AsyncMock(return_value=True), ensure_protection=AsyncMock()
    )
    ledger = SimpleNamespace(get_open=lambda: rows, set_stop_order_id=Mock())
    await reconcile_protective_orders(
        broker, ledger, execution=execution, report=ReconciliationReport()
    )
    execution.cancel_protection.assert_awaited_once_with(
        broker_order_id="BWA", symbol="BWA", reason="owner_automatic_stop_disabled"
    )
    execution.ensure_protection.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconciliation_restores_missing_new_stop_without_touching_legacy(monkeypatch):
    monkeypatch.setattr(get_settings(), "paper_exit_policy", "manual_target")
    protected = SimpleNamespace(
        id=uuid4(),
        symbol="AAPL",
        qty=D(5),
        stop_price=D(99),
        payload={"position_exit_policy": PROTECTED_INTRADAY},
    )
    legacy = SimpleNamespace(symbol="BWA", payload={})
    broker = SimpleNamespace(
        list_open_orders=AsyncMock(return_value=[]),
        list_positions=AsyncMock(return_value=[protected]),
    )
    ledger = SimpleNamespace(get_open=lambda: [protected, legacy], set_stop_order_id=Mock())
    execution = SimpleNamespace(ensure_protection=AsyncMock(return_value="restored"))
    assert await reconcile_protective_orders(broker, ledger, execution=execution) == 1
    execution.ensure_protection.assert_awaited_once_with(
        symbol="AAPL",
        qty=D(5),
        stop_price=D(99),
        position_id=protected.id,
        reason="reconcile: protective stop missing at broker",
    )
    ledger.set_stop_order_id.assert_called_once_with(protected.id, "restored")
