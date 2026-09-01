"""A desk that has stopped checking itself against the broker must say so.

Reconciliation is best-effort enrichment of the desk payload — a vendor hiccup
should degrade the view, not fail the request. But "degrade" has to mean
something the operator can see. A warning in the server log while `/desk/broker`
answers 200 with a full set of numbers is indistinguishable from a healthy
desk, and the numbers in that state are the local book's opinion rather than
broker truth.
"""

from __future__ import annotations

import pytest

from api.routes import desk as desk_mod


@pytest.fixture(autouse=True)
def clean_snapshot_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with no cached snapshot and no remembered outcome."""
    from trading.reconcile_supervisor import RECONCILE

    monkeypatch.setattr(desk_mod, "_broker_cache", None)
    monkeypatch.setattr(desk_mod, "_broker_cache_mono", 0.0)
    RECONCILE.reset()


def _stub_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace everything the snapshot touches except reconciliation itself."""

    class _Broker:
        async def get_portfolio(self):
            raise RuntimeError("not needed")

        async def list_positions(self):
            return []

        async def list_open_orders(self):
            return []

    monkeypatch.setattr(desk_mod, "create_broker", lambda _s: _Broker())
    monkeypatch.setattr(desk_mod, "create_audit", lambda: None)
    monkeypatch.setattr(desk_mod, "build_execution_service", lambda **_kw: None)
    # No market data port and no exit stub: the assessment moved to
    # `agents.position.loop`, so this handler no longer reads market data.


async def test_a_failing_reconcile_is_reported_in_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact failure is carried, so the desk can name it rather than hint."""
    _stub_dependencies(monkeypatch)

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("no such column: order_intents.purpose")

    monkeypatch.setattr(desk_mod, "reconcile_positions", _boom)

    snap = await desk_mod._build_broker_snapshot(force=True)

    assert snap["reconciliation"]["ok"] is False
    assert "order_intents.purpose" in snap["reconciliation"]["error"]
    assert snap["reconciliation"]["last_success_at"] is None


async def test_a_successful_reconcile_clears_the_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery must be visible too, or the banner would never come down."""
    _stub_dependencies(monkeypatch)

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(desk_mod, "reconcile_positions", _boom)
    failed = await desk_mod._build_broker_snapshot(force=True)
    assert failed["reconciliation"]["ok"] is False

    async def _fine(*_a: object, **_k: object) -> None:
        return None

    monkeypatch.setattr(desk_mod, "reconcile_positions", _fine)
    recovered = await desk_mod._build_broker_snapshot(force=True)

    assert recovered["reconciliation"]["ok"] is True
    assert recovered["reconciliation"]["error"] is None
    assert recovered["reconciliation"]["last_success_at"] is not None
    assert recovered["reconciliation"]["stale_seconds"] is not None


async def test_the_request_still_succeeds_while_reconciliation_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Surfacing the failure must not become a way to take the desk down."""
    _stub_dependencies(monkeypatch)

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(desk_mod, "reconcile_positions", _boom)

    snap = await desk_mod._build_broker_snapshot(force=True)

    assert "positions" in snap
    assert "open_orders" in snap


async def test_the_flag_survives_model_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """`BrokerSnapshot` is the wire contract; a field it drops never ships."""
    _stub_dependencies(monkeypatch)

    async def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(desk_mod, "reconcile_positions", _boom)
    snap = await desk_mod._build_broker_snapshot(force=True)

    validated = desk_mod.BrokerSnapshot.model_validate(snap)

    assert validated.reconciliation["ok"] is False
