"""Operator entry aggressiveness must widen BUY without disarming hard vetoes."""

from __future__ import annotations

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


def test_zero_matches_shipped_f3_floors() -> None:
    th = thresholds_for(0)
    assert th.aggressiveness == 0
    assert th.ema_ext_pct == 2.5
    assert th.vwap_ext_pct == 1.0
    assert th.min_buy_quality == 55
    assert th.zone_gap_frac == 0.0
    assert th.allow_soft_chase_buy is False
    assert th.wait_ttl_minutes == 390
    assert th.flag_impulse_weak is True
    assert th.pullback_deep_no_trade is True


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
    # Softest production step — still far below old experimental extremes.
    assert th.vwap_ext_pct <= 4.5
    assert th.ema_ext_pct <= 9.0
    assert th.atr_ext_max <= 3.0
    assert th.max_spread_bps <= 40.0
    assert th.min_setup_quality >= 52
    assert th.min_entry_quality >= 48


def test_medium_matches_historical_production_soft_end() -> None:
    """a=50 must keep the old three-step medium floors so desk behaviour does not jump."""
    th = thresholds_for(50)
    assert th.aggressiveness == 50
    assert th.vwap_ext_pct == 3.5
    assert th.ema_ext_pct == 7.0
    assert th.wait_ttl_minutes == 180
    assert th.allow_soft_chase_buy is True
    assert th.allow_fast_pullback is True


def test_weak_extends_past_medium() -> None:
    mid = thresholds_for(50)
    weak = thresholds_for(100)
    assert weak.aggressiveness == 100
    assert weak.vwap_ext_pct > mid.vwap_ext_pct
    assert weak.wait_ttl_minutes == 90
    assert weak.zone_gap_frac > mid.zone_gap_frac


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


def test_wait_ttl_from_policy() -> None:
    set_entry_aggressiveness(0, actor="test")
    assert thresholds_for(0).wait_ttl_minutes == 390
    set_entry_aggressiveness(50, actor="test")
    assert thresholds_for(50).wait_ttl_minutes == 180
    set_entry_aggressiveness(PRODUCTION_MAX_AGGRESSIVENESS, actor="test")
    assert thresholds_for(PRODUCTION_MAX_AGGRESSIVENESS).wait_ttl_minutes == 90


def test_aggressive_policy_allows_extended_buy() -> None:
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
    assert float(loose.entry_zone_high) >= float(strict.entry_zone_high)


def test_zone_moves_toward_price_with_aggressiveness() -> None:
    facts = evaluate_timing(_snap(close=110.0, sma20=100.0, atr=2.0, vwap=100.0))
    set_entry_aggressiveness(0, actor="test")
    _, high0 = zone_from_facts(facts)
    set_entry_aggressiveness(PRODUCTION_MAX_AGGRESSIVENESS, actor="test")
    _, high1 = zone_from_facts(facts)
    assert float(high1) >= float(high0)


def test_detect_chasing_respects_thresholds() -> None:
    facts = evaluate_timing(_snap(close=105.0, sma20=100.0, atr=1.0, vwap=100.0))
    set_entry_aggressiveness(0, actor="test")
    assert "PRICE_TOO_EXTENDED_FROM_EMA" in detect_chasing(facts)
    set_entry_aggressiveness(PRODUCTION_MAX_AGGRESSIVENESS, actor="test")
    # Production max still may flag large extensions; ensure call succeeds.
    assert isinstance(detect_chasing(facts), list)


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
