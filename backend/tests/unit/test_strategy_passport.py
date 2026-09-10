"""The passport reflects runtime parameters without registry promotion writes."""
from api.routes.strategies import active_strategy
from strategy.orb import PARAMETERS, VERSION


def test_passport_uses_active_policy(monkeypatch):
    from strategy.orb import evaluation

    monkeypatch.setattr(evaluation, "paper_evaluation", lambda: {"trade_count": 0})
    result = active_strategy()
    assert result["version"] == VERSION
    assert result["parameters"] == PARAMETERS
    assert result["parameters"] is not PARAMETERS
    assert result["paper"]["trade_count"] == 0
    assert result["historical_backtest"] == "not_implemented"
    assert result["live_readiness"] == "not_certified"
