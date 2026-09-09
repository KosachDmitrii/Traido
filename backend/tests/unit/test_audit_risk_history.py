import pytest

from core.enums import RiskVerdict
from core.schemas import RiskLimits
from risk.risk_engine import RiskEngine
from tests.unit.test_capital_safety import _candidate, _portfolio


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"week_pnl": None}, ["WEEKLY_PNL_UNAVAILABLE"]),
        ({"drawdown_pct": None}, ["PORTFOLIO_DRAWDOWN_UNAVAILABLE"]),
        (
            {"week_pnl": None, "drawdown_pct": None},
            ["WEEKLY_PNL_UNAVAILABLE", "PORTFOLIO_DRAWDOWN_UNAVAILABLE"],
        ),
    ],
)
def test_unknown_risk_history_is_not_zero_loss(overrides, expected):
    result = RiskEngine(RiskLimits()).evaluate(_candidate(), _portfolio(**overrides))
    assert result.verdict != RiskVerdict.PASS
    assert result.reasons == expected
