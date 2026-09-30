"""Repair only unclaimed metadata; preserve all consumed exposure identities."""

from copy import deepcopy

from strategy.orb.store import create_session, restore_unclaimed_instruments
from tests.unit.test_orb_retest import scenario


def test_restore_uses_provider_facts_and_never_rewrites_claimed_plans():
    plan, _, _ = scenario()
    raw = plan.model_dump(mode="json")
    claimed = deepcopy(raw)
    claimed["symbol"] = "CLAIMED"
    create_session(
        plan.session,
        {
            "plans": {plan.symbol: raw, "CLAIMED": claimed},
            "states": {"CLAIMED": {"opportunity_id": "unresolved-claim", "state": "UNKNOWN"}},
        },
    )
    fact = {"asset_class": "etf", "provider": "alpaca", "as_of": plan.range_end.isoformat()}
    repaired = restore_unclaimed_instruments(plan.session, {plan.symbol: fact, "CLAIMED": fact})
    assert repaired["plans"][plan.symbol]["evidence"]["instrument"] == fact
    assert repaired["plans"]["CLAIMED"] == claimed
    assert repaired["states"]["CLAIMED"]["state"] == "UNKNOWN"
    for key in ("stop", "trigger", "max_entry", "entry_deadline"):
        assert repaired["plans"][plan.symbol][key] == raw[key]
    repeated = restore_unclaimed_instruments(plan.session, {plan.symbol: fact})
    assert len(repeated["instrument_provenance_repairs"]) == 1
