"""DecisionPipeline must sit on the new-exposure path; claim must be DB CAS."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_execution_imports_decision_pipeline_gate_order() -> None:
    tree = ast.parse((ROOT / "trading" / "execution.py").read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "trading.decision_pipeline":
            names.update(a.name for a in node.names if a.name)
    assert "NEW_EXPOSURE_GATE_ORDER" in names


def test_new_exposure_gate_order_is_complete() -> None:
    from trading.decision_pipeline import NEW_EXPOSURE_GATE_ORDER

    required = {
        "kill_switch",
        "data_configuration",
        "data_freshness",
        "instrument_eligibility",
        "broker_health",
        "reconciliation_freshness",
        "market_hours",
        "corporate_action",
        "event_risk",
        "liquidity",
        "portfolio_exposure",
        "risk_engine",
    }
    assert set(NEW_EXPOSURE_GATE_ORDER) == required
    assert NEW_EXPOSURE_GATE_ORDER.index("risk_engine") == len(NEW_EXPOSURE_GATE_ORDER) - 1


def test_intent_transition_from_is_compare_and_swap() -> None:
    from core.enums import IntentStatus, OrderSide, OrderType
    from trading.intents import MemoryOrderIntentStore
    from trading.order_intent import OrderIntent

    store = MemoryOrderIntentStore()
    intent = OrderIntent(
        idempotency_key="k1",
        broker="test",
        symbol="AAPL",
        side=OrderSide.BUY,
        requested_qty=__import__("decimal").Decimal(1),
        order_type=OrderType.LIMIT,
    )
    store.create_or_get(intent)
    first = store.transition_from(
        intent.id,
        from_status=IntentStatus.CREATED,
        to_status=IntentStatus.SUBMITTING,
        client_order_id="c1",
    )
    second = store.transition_from(
        intent.id,
        from_status=IntentStatus.CREATED,
        to_status=IntentStatus.SUBMITTING,
        client_order_id="c2",
    )
    assert first is not None
    assert second is None
