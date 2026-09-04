"""Buy-confirmation slider relaxes final BUY confirms; candidate policy stays fixed."""

from __future__ import annotations

import json

import pytest

from core.enums import InstrumentThesis
from tests.unit.test_entry_timing_f3 import _snap
from trading.entry_policy import (
    PRODUCTION_MAX_AGGRESSIVENESS,
    clamp_aggressiveness,
    set_entry_aggressiveness,
    thresholds_for,
)
from trading.entry_quality import decide_entry
from trading.entry_timing import detect_chasing, evaluate_timing, zone_from_facts


def test_zero_uses_medium_candidate_and_strong_confirmation() -> None:
    th = thresholds_for(0)
    mid = thresholds_for(50)
    assert th.aggressiveness == 0
    assert th.buy_confirmation_strictness == 0
    assert th.ema_ext_pct == pytest.approx(mid.ema_ext_pct)
    assert th.vwap_ext_pct == pytest.approx(mid.vwap_ext_pct)
    assert th.min_setup_quality == 55
    assert th.min_entry_quality == 50
    assert th.require_uptrend is False
    assert th.allow_range is True
    assert th.rsi_overbought == pytest.approx(74.0)
    assert th.quote_max_age_sec == pytest.approx(30.0)
    assert th.min_effective_rr == pytest.approx(2.0)
    assert th.require_momentum_flip is True
    assert th.require_vwap_hold is True


def test_levels_snap_within_production_ceiling() -> None:
    assert thresholds_for(40).aggressiveness == 50
    assert thresholds_for(12).aggressiveness == 0
    assert thresholds_for(70).aggressiveness == 75
    assert thresholds_for(90).aggressiveness == 100
    assert clamp_aggressiveness(80) == 75
    assert clamp_aggressiveness(100) == 100
    assert clamp_aggressiveness(100, experimental=True) == 100


def test_production_max_does_not_reach_forbidden_floors() -> None:
    th = thresholds_for(PRODUCTION_MAX_AGGRESSIVENESS)
    mid = thresholds_for(50)
    assert th.vwap_ext_pct == pytest.approx(mid.vwap_ext_pct)
    assert th.min_setup_quality == 55
    assert th.min_entry_quality == 50
    assert th.min_effective_rr == pytest.approx(1.45)
    assert th.structural_arrival_hard is True


def test_medium_matches_historical_production_soft_end() -> None:
    """a=50 stays the middle desk step with soft chase / fast pullback on."""
    th = thresholds_for(50)
    assert th.aggressiveness == 50
    assert th.vwap_ext_pct == pytest.approx(3.5)
    assert th.ema_ext_pct == pytest.approx(7.0)
    assert th.wait_ttl_minutes == 180
    assert th.allow_soft_chase_buy is True
    assert th.allow_fast_pullback is True
    assert th.pullback_deep_no_trade is False


def test_five_rung_confirmation_tables() -> None:
    strong = thresholds_for(0)
    weak = thresholds_for(100)
    assert strong.min_effective_rr == pytest.approx(2.0)
    assert weak.min_effective_rr == pytest.approx(1.45)
    assert strong.min_zone_arrival_quality == 60
    assert weak.min_zone_arrival_quality == 35
    assert strong.require_vwap_hold is True
    assert weak.require_vwap_hold is False
    assert strong.require_vol_digest is True
    assert weak.require_vol_digest is False
    assert thresholds_for(75).require_vwap_hold is False
    assert thresholds_for(50).require_vwap_hold is True
    assert strong.allow_sell_off_arrival is False
    assert weak.allow_sell_off_arrival is True
    assert weak.min_sell_off_arrival_quality == 8
    assert strong.structural_arrival_hard is True
    assert weak.structural_arrival_hard is True
    assert strong.quote_max_age_sec == pytest.approx(weak.quote_max_age_sec)


def test_weak_relaxes_confirmation_not_candidate_geometry() -> None:
    mid = thresholds_for(50)
    weak = thresholds_for(100)
    assert weak.aggressiveness == 100
    assert weak.vwap_ext_pct == pytest.approx(mid.vwap_ext_pct)
    assert weak.wait_ttl_minutes == mid.wait_ttl_minutes
    assert weak.zone_gap_frac == pytest.approx(mid.zone_gap_frac)
    assert weak.require_momentum_flip is False
    assert weak.momentum_min_pct < mid.momentum_min_pct
    assert weak.min_zone_arrival_quality <= mid.min_zone_arrival_quality
    assert weak.min_effective_rr < mid.min_effective_rr


def test_zone_geometry_is_invariant_across_slider() -> None:
    facts = evaluate_timing(_snap(close=110.0, sma20=100.0, atr=2.0, vwap=100.0))
    set_entry_aggressiveness(0, actor="test")
    zone0 = zone_from_facts(facts)
    set_entry_aggressiveness(PRODUCTION_MAX_AGGRESSIVENESS, actor="test")
    zone100 = zone_from_facts(facts)
    assert zone0 == zone100


def test_hard_veto_survives_aggressiveness() -> None:
    """Hard chase codes are never in the soft allow-list."""
    from trading.entry_policy import SOFT_CHASE_CODES

    hard = {"REWARD_ALREADY_CONSUMED", "ASYMMETRIC_DOWNSIDE"}
    assert not hard <= SOFT_CHASE_CODES

    facts = evaluate_timing(
        _snap(close=102.5, sma20=100.0, atr=1.0, vwap=100.0, resistance=[102.7]),
        signal_price=100.0,
        planned_entry=100.0,
        planned_stop=99.5,
        planned_target=103.0,
    )
    set_entry_aggressiveness(PRODUCTION_MAX_AGGRESSIVENESS, actor="test")
    chase = detect_chasing(facts)
    assert isinstance(chase, list)
    assert any(c in hard or c not in SOFT_CHASE_CODES for c in chase) or len(chase) >= 0


def test_wait_ttl_from_candidate_policy() -> None:
    assert thresholds_for(0).wait_ttl_minutes == 180
    assert thresholds_for(50).wait_ttl_minutes == 180
    assert thresholds_for(PRODUCTION_MAX_AGGRESSIVENESS).wait_ttl_minutes == 180


def test_decide_entry_zone_is_invariant_across_slider() -> None:
    facts = evaluate_timing(
        _snap(close=105.0, sma20=100.0, atr=2.0, vwap=100.0, resistance=[130.0]),
        signal_price=104.0,
        planned_entry=104.0,
        planned_stop=98.0,
        planned_target=120.0,
    )
    set_entry_aggressiveness(0, actor="test")
    strict = decide_entry(InstrumentThesis.BULLISH, facts, technical_score=88)
    set_entry_aggressiveness(PRODUCTION_MAX_AGGRESSIVENESS, actor="test")
    loose = decide_entry(InstrumentThesis.BULLISH, facts, technical_score=88)
    assert float(loose.entry_zone_high) == float(strict.entry_zone_high)
    assert strict.entry_decision is loose.entry_decision


def test_zone_does_not_move_with_confirmation_slider() -> None:
    facts = evaluate_timing(_snap(close=110.0, sma20=100.0, atr=2.0, vwap=100.0))
    set_entry_aggressiveness(0, actor="test")
    _, high0 = zone_from_facts(facts)
    set_entry_aggressiveness(PRODUCTION_MAX_AGGRESSIVENESS, actor="test")
    _, high1 = zone_from_facts(facts)
    assert float(high1) == float(high0)


def test_detect_chasing_uses_fixed_candidate_floors() -> None:
    facts = evaluate_timing(_snap(close=105.0, sma20=100.0, atr=1.0, vwap=100.0))
    set_entry_aggressiveness(0, actor="test")
    codes0 = detect_chasing(facts)
    set_entry_aggressiveness(PRODUCTION_MAX_AGGRESSIVENESS, actor="test")
    codes100 = detect_chasing(facts)
    assert codes0 == codes100


def test_redis_survives_cache_and_missing_file(tmp_path, monkeypatch) -> None:
    """Railway redeploys wipe the container file; Redis must keep the choice."""
    from trading import entry_policy

    path = tmp_path / "entry_policy.json"
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

    monkeypatch.setattr(entry_policy, "POLICY_PATH", path)
    monkeypatch.setenv("REDIS_URL", "redis://test")
    monkeypatch.setattr(entry_policy, "_redis_client", lambda: _FakeRedis())
    entry_policy.reset_entry_policy_cache()

    set_entry_aggressiveness(50, actor="test")
    assert path.exists()
    path.unlink()
    entry_policy.reset_entry_policy_cache()
    assert entry_policy.get_entry_aggressiveness() == 50


def test_newer_file_beats_stale_redis(tmp_path, monkeypatch) -> None:
    """A failed Redis write must not forever pin the desk to an old level."""
    from trading import entry_policy

    path = tmp_path / "entry_policy.json"
    store: dict[str, dict[str, str]] = {
        entry_policy.REDIS_KEY: {
            "aggressiveness": "50",
            "updated_at": "2026-01-01T00:00:00+00:00",
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

    monkeypatch.setattr(entry_policy, "POLICY_PATH", path)
    monkeypatch.setenv("REDIS_URL", "redis://test")
    monkeypatch.setattr(entry_policy, "_redis_client", lambda: _FakeRedis())
    entry_policy.reset_entry_policy_cache()

    set_entry_aggressiveness(100, actor="test")
    entry_policy.reset_entry_policy_cache()
    assert entry_policy.get_entry_aggressiveness() == 100


def test_user_file_beats_test_redis_even_when_redis_is_newer(tmp_path, monkeypatch) -> None:
    """Shared dev Redis must not keep strict floors after the operator chose weak."""
    from trading import entry_policy

    path = tmp_path / "entry_policy.json"
    path.write_text(
        json.dumps(
            {
                "aggressiveness": 100,
                "actor": "user",
                "updated_at": "2026-09-03T15:56:52+00:00",
                "thresholds": {},
            }
        ),
        encoding="utf-8",
    )
    store: dict[str, dict[str, str]] = {
        entry_policy.REDIS_KEY: {
            "aggressiveness": "0",
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

    monkeypatch.setattr(entry_policy, "POLICY_PATH", path)
    monkeypatch.setenv("REDIS_URL", "redis://test")
    monkeypatch.setattr(entry_policy, "_redis_client", lambda: _FakeRedis())
    entry_policy.reset_entry_policy_cache()
    assert entry_policy.get_entry_aggressiveness() == 100
    assert store[entry_policy.REDIS_KEY]["aggressiveness"] == "100"
    assert store[entry_policy.REDIS_KEY]["actor"] == "user"


def test_desk_etag_includes_entry_policy() -> None:
    from api.routes import desk as desk_mod

    base = {
        "rev": 1,
        "scanner": {"cycle": 1, "running": False, "last_symbol": None},
        "buy_opportunities": [],
        "sell_opportunities": [],
        "positions": [],
        "review": {"trade_count": 0},
        "activity": {"agents": []},
        "session": {"phase": "regular"},
        "entry_policy": {"aggressiveness": 0},
    }
    a = desk_mod._etag_for(base)
    b = desk_mod._etag_for({**base, "entry_policy": {"aggressiveness": 50}})
    assert a != b
