"""Does the harness reach the real thing?

Before any gate assertion is worth reading, two facts have to hold: the desk
under test is wired the way production wires it, and the tape really does see
what leaves for the broker. A harness that quietly substituted the execution
service would make every later test in this directory green and meaningless.
"""

from __future__ import annotations

import pytest


def test_the_app_answers_over_the_real_route(desk) -> None:
    r = desk.client.get("/api/v1/opportunities")
    assert r.status_code == 200
    assert r.json() == []


def test_the_composition_root_arms_the_liquidity_gate(desk) -> None:
    """§4.1 of the audit, asserted against the running app rather than in isolation."""
    from api.deps import build_execution_service

    service = build_execution_service()
    assert service.market_data is not None, "the liquidity gate has nothing to measure"
    assert service.quotes is not None, "the spread check has no top of book"


def test_a_seeded_card_is_a_real_risk_decision(desk) -> None:
    opp = desk.offer("AAPL")

    listed = desk.client.get("/api/v1/opportunities").json()
    assert [o["id"] for o in listed] == [str(opp.id)]
    assert opp.risk.verdict.value == "pass", opp.risk.reasons
    assert opp.risk.sized_qty and opp.risk.sized_qty > 0


def test_nothing_reaches_the_broker_before_a_decision(desk) -> None:
    desk.offer("AAPL")
    desk.assert_no_broker_mutations()


@pytest.mark.parametrize("path", ["/api/v1/desk", "/health"])
def test_supporting_routes_still_answer(desk, path: str) -> None:
    assert desk.client.get(path).status_code == 200
