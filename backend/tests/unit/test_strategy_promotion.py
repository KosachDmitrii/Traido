"""Stage 8 strategy registry + promotion gate."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

import strategy.promotion as promo_mod
from database.models.journal import BacktestRunRow, TradeJournalRow
from database.models.strategy import StrategyEvaluationRunRow
from database.session import session_factory
from strategy import StrategyPromotionStage
from strategy.promotion import (
    PromotionError,
    human_approve,
    promote_to_production,
    recompute_version,
    reject_version,
)
from strategy.registry import (
    LIVE_STRATEGY_KEY,
    RESEARCH_STRATEGY_KEY,
    ensure_builtin_strategies,
    get_by_key,
    register_version,
)
from strategy.thresholds import PromotionThresholds


@pytest.fixture()
def loose_thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    thr = PromotionThresholds(
        min_backtest_trades=1,
        min_backtest_return_pct=-100.0,
        min_oos_trades=1,
        min_oos_return_pct=-100.0,
        min_profit_factor=0.0,
        min_walk_forward_efficiency=0.0,
        min_paper_trades=2,
        min_paper_expectancy_usd=-1000.0,
        min_paper_profit_factor=0.0,
        min_regimes_with_trades=1,
    )
    monkeypatch.setattr(promo_mod, "get_promotion_thresholds", lambda: thr)


def test_builtin_strategies_register_immutably(loose_thresholds) -> None:
    ensure_builtin_strategies()
    ensure_builtin_strategies()
    assert get_by_key(LIVE_STRATEGY_KEY) is not None
    assert get_by_key(RESEARCH_STRATEGY_KEY) is not None
    with pytest.raises(ValueError, match="immutable"):
        register_version(
            key=LIVE_STRATEGY_KEY,
            name="trader_desk",
            version_tag="1.2.0",
            parameters={"tampered": True},
        )


def test_promotion_chain_to_production(loose_thresholds) -> None:
    ensure_builtin_strategies()
    row = get_by_key(RESEARCH_STRATEGY_KEY)
    assert row is not None
    vid = row["id"]

    with session_factory()() as session:
        session.add(
            StrategyEvaluationRunRow(
                strategy_version_key=RESEARCH_STRATEGY_KEY,
                symbol="SPY",
                timeframe="D1",
                generated_at=datetime.now(UTC),
                verdict="PASS",
                payload={
                    "out_of_sample": {
                        "trade_count": 25,
                        "return_pct": 12.0,
                        "profit_factor": 1.5,
                        "walk_forward_efficiency": 0.6,
                    },
                    "by_regime": [{"regime": "bull", "trade_count": 10}],
                    "trade_count": 40,
                    "return_pct": 15.0,
                },
            )
        )
        session.add(
            BacktestRunRow(
                strategy_version=RESEARCH_STRATEGY_KEY,
                symbol="SPY",
                timeframe="D1",
                starting_equity=Decimal(100000),
                ending_equity=Decimal(110000),
                net_pnl=Decimal(10000),
                return_pct=10.0,
                trade_count=40,
                win_count=22,
                loss_count=18,
                win_rate=0.55,
                profit_factor=1.4,
                max_drawdown_pct=8.0,
                avg_r=0.2,
                avg_bars_held=5.0,
                params={},
            )
        )
        for pnl in (Decimal(50), Decimal(25), Decimal(-10)):
            session.add(
                TradeJournalRow(
                    symbol="AAA",
                    entry=Decimal(10),
                    exit=Decimal(11),
                    qty=Decimal(1),
                    pnl=pnl,
                    pnl_pct=float(pnl),
                    entry_reasons=[],
                    exit_reasons=[],
                    strategy_version=RESEARCH_STRATEGY_KEY,
                    trading_mode="confirmation",
                )
            )
        session.commit()

    recomputed = recompute_version(vid)
    assert recomputed["stage"] == StrategyPromotionStage.PAPER_PASSED.value

    approved = human_approve(vid, actor="test")
    assert approved["stage"] == StrategyPromotionStage.HUMAN_APPROVED.value
    assert approved["approved_by"] == "test"

    production = promote_to_production(vid, actor="test")
    assert production["stage"] == StrategyPromotionStage.PRODUCTION.value


def test_approve_blocked_before_paper(loose_thresholds) -> None:
    ensure_builtin_strategies()
    vid = get_by_key(LIVE_STRATEGY_KEY)["id"]
    with pytest.raises(PromotionError, match="paper gate"):
        human_approve(vid, actor="test")


def test_reject_blocks_recompute(loose_thresholds) -> None:
    ensure_builtin_strategies()
    vid = get_by_key(LIVE_STRATEGY_KEY)["id"]
    reject_version(vid, actor="test", reason="no edge")
    with pytest.raises(PromotionError, match="rejected"):
        recompute_version(vid)
