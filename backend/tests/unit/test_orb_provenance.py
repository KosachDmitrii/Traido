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


def test_correct_misclassified_etf_without_replacing_stock_with_unverified_fact():
    plan, _, _ = scenario()
    raw = plan.model_dump(mode="json")
    raw["evidence"]["instrument"] = {"asset_class": "stock", "provider": "alpaca"}
    create_session(plan.session, {"plans": {plan.symbol: raw}})
    facts = {plan.symbol: {"asset_class": "etf", "provider": "alpaca"}}
    repaired = restore_unclaimed_instruments(plan.session, facts)
    assert repaired["plans"][plan.symbol]["evidence"]["instrument"]["asset_class"] == "etf"
    assert repaired["instrument_classification_revision"] == "alpaca-name-etf@1"


def test_alpaca_etf_names_without_an_etf_attribute():
    from universe.models import AssetClass
    from universe.provider import _instrument_from_alpaca

    for name, expected in [
        ("iShares National Muni Bond ETF", AssetClass.ETF),
        ("State Street SPDR S&P 500 ETF Trust", AssetClass.ETF),
        ("ETF Capital Management Inc", AssetClass.STOCK),
        ("Some Investment Trust", AssetClass.STOCK),
        ("Some Exchange Traded Note ETN", AssetClass.STOCK),
    ]:
        instrument = _instrument_from_alpaca({"symbol": "TEST", "class": "us_equity", "name": name})
        assert instrument.asset_class is expected


def test_restore_incomplete_instrument_from_provider():
    plan, _, _ = scenario()
    raw = plan.model_dump(mode="json")
    raw["evidence"]["instrument"] = {"provider": "alpaca"}
    create_session(plan.session, {"plans": {plan.symbol: raw}})
    fact = {"asset_class": "etf", "provider": "alpaca"}
    repaired = restore_unclaimed_instruments(plan.session, {plan.symbol: fact})
    assert repaired["plans"][plan.symbol]["evidence"]["instrument"] == fact


async def test_restart_repairs_partial_instrument_even_at_current_revision():
    from types import SimpleNamespace

    from strategy.orb.runtime import discover
    from tests.unit.test_orb_session import NOW, Context
    from universe.models import AssetClass
    from universe.provider import ALPACA_CLASSIFICATION_REVISION, _instrument_from_alpaca

    instrument = _instrument_from_alpaca(
        {"symbol": "EZU", "class": "us_equity", "name": "iShares MSCI Eurozone ETF"}
    )
    assert instrument.asset_class is AssetClass.ETF

    class Universe:
        async def get_scan_universe(self, **kwargs):
            return SimpleNamespace(symbols=["EZU"], eligible=[instrument], total=1)

    original = await discover(Context(["EZU"]), Universe(), now=NOW)
    from database.models.orb import OrbSessionRow
    from database.session import session_factory

    with session_factory()() as db:
        row = db.get(OrbSessionRow, original["session"])
        payload = deepcopy(row.payload)
        payload["plans"]["EZU"]["evidence"]["instrument"] = {"provider": "alpaca"}
        assert payload["instrument_classification_revision"] == ALPACA_CLASSIFICATION_REVISION
        row.payload = payload
        db.commit()
    repaired = await discover(Context([]), Universe(), now=NOW)
    assert repaired["plans"]["EZU"]["evidence"]["instrument"]["asset_class"] == "etf"
    for key in ("stop", "trigger", "max_entry", "entry_deadline"):
        assert repaired["plans"]["EZU"][key] == original["plans"]["EZU"][key]


def test_policy_rollout_preserves_instrument_without_reusing_retest():
    from core.schemas import Bar
    from strategy.orb import form_plan
    from strategy.orb.store import upgrade_unpublished_entry_limits
    from tests.unit.test_orb_policy import NOW, evidence

    daily, opening = evidence()
    daily = [b.model_copy(update={"symbol": "EZU"}) for b in daily]
    opening = [b.model_copy(update={"symbol": "EZU"}) for b in opening]
    plan = form_plan("EZU", daily, opening, now=NOW, feed="sip", version="orb@1.1.0").plan
    raw = plan.model_dump(mode="json")
    instrument = {"asset_class": "etf", "provider": "alpaca", "as_of": NOW.isoformat()}
    raw["evidence"]["instrument"] = instrument
    raw["evidence"]["retest"] = {"phase": "ready", "target": "999"}
    create_session(plan.session, {"plans": {"EZU": raw}})
    upgraded = upgrade_unpublished_entry_limits(plan.session, now=NOW)
    saved = upgraded["plans"]["EZU"]
    assert saved["evidence"]["instrument"] == instrument
    assert "retest" not in saved["evidence"]
    assert saved["evidence"]["daily"] == [
        Bar.model_validate(b).model_dump(mode="json") for b in raw["evidence"]["daily"]
    ]
