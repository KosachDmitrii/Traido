"""Historical diagnosis cannot use future bars or turn a setup into a fill."""

from datetime import timedelta

from database.models.desk import OrderIntentRow
from database.models.orb import OrbDecisionEventRow
from database.session import session_factory
from market_data.bar_store import save_bars
from strategy.orb.replay import replay_plan, session_replay
from strategy.orb.store import create_session, update_state
from tests.unit.test_orb_retest import scenario


def test_replay_uses_only_completed_prefix_and_deduplicates_signal():
    plan, rows, now = scenario()
    report = replay_plan(plan, rows, as_of=now - timedelta(seconds=1))
    assert report["signals"] == []
    report = replay_plan(plan, rows, as_of=now)
    assert len(report["signals"]) == 1
    assert report["signals"][0]["confirmed_at"] == now.isoformat()
    assert report["signals"][0]["target"] == "102.00"
    future = rows[-1].model_copy(update={"ts": now})
    assert replay_plan(plan, [*rows, future], as_of=now) == report
    later = replay_plan(plan, [*rows, future], as_of=now + timedelta(minutes=5))
    assert len(later["signals"]) == 1


def test_gap_or_absent_history_is_reported_without_inventing_signal():
    plan, rows, now = scenario()
    for values in ([], [rows[0], rows[2]]):
        report = replay_plan(plan, values, as_of=now)
        assert report["missing_intervals"] > 0
        assert report["signals"] == []


def test_session_report_is_read_only_and_labels_its_limits():
    plan, rows, now = scenario()
    create_session(plan.session, {"plans": {plan.symbol: plan.model_dump(mode="json")}})
    save_bars(plan.source, plan.symbol, rows)
    report = session_replay(plan.range_start.date(), as_of=now)
    assert report["confirmed_setups"] == 1
    assert report["plans_with_missing_intervals"] == 0
    assert report["quote_admission_replayed"] is False
    assert report["fills_simulated"] is False
    with session_factory()() as db:
        assert db.query(OrderIntentRow).count() == 0
        assert db.query(OrbDecisionEventRow).count() == 0


def test_recorded_reason_summary_does_not_require_replaying_bars():
    plan, _, now = scenario()
    create_session(plan.session, {"plans": {plan.symbol: plan.model_dump(mode="json")}})
    for _ in range(2):
        update_state(plan.session, plan.symbol, {"state": "BLOCKED", "reasons": ["REGIME_STALE"]})
    report = session_replay(plan.range_start.date(), as_of=now, replay_bars=False)
    assert report["status"] == "recorded_observations"
    assert report["recorded_observation_states"] == {"BLOCKED": 2}
    assert report["recorded_observation_reasons"] == {"REGIME_STALE": 2}
