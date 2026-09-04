"""
Capital safety invariants.

These are the rules from AGENTS.md expressed as tests. They are separated from
the rest of the suite and run as their own CI job on purpose: a green "all
tests passed" that quietly hid a live-order path would be worse than a red
build.

If a change makes one of these fail, the change is wrong — not the test.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from core.config import Settings
from core.enums import (
    BrokerEnvironment,
    EarningsCheck,
    NewsCheck,
    RiskVerdict,
    SectorCheck,
    TradeAction,
)
from core.schemas import PortfolioSnapshot, RiskLimits, TradeCandidate
from risk.limits import load_risk_limits
from risk.risk_engine import RiskContext, RiskEngine
from tests.support import CLEARED_EARNINGS

REPO = Path(__file__).resolve().parents[2]
SOURCE_DIRS = (
    "agents",
    "api",
    "broker",
    "core",
    "database",
    "market_data",
    "quant",
    "risk",
    "trading",
)


def _candidate(**over) -> TradeCandidate:  # type: ignore[no-untyped-def]
    base = {
        "symbol": "AAPL",
        "action": TradeAction.BUY,
        "entry": Decimal(100),
        "stop": Decimal(95),
        "target": Decimal(115),
        "confidence": 0.8,
        "risk_reward": 3.0,
        "reasons": ["test"],
        "strategy_version": "test@1",
    }
    base.update(over)
    return TradeCandidate(**base)  # type: ignore[arg-type]


def _portfolio(**over) -> PortfolioSnapshot:  # type: ignore[no-untyped-def]
    base = {
        "equity": Decimal(100000),
        "cash": Decimal(100000),
        "buying_power": Decimal(100000),
        "open_exposure": Decimal(0),
        "open_positions": 0,
        "day_pnl": Decimal(0),
        "week_pnl": Decimal(0),
        "drawdown_pct": 0.0,
        "kill_switch": False,
    }
    base.update(over)
    return PortfolioSnapshot(**base)  # type: ignore[arg-type]


def _python_sources() -> list[Path]:
    files: list[Path] = []
    for d in SOURCE_DIRS:
        files.extend((REPO / d).rglob("*.py"))
    return [f for f in files if "__pycache__" not in f.parts]


# ── The kill switch is absolute ──────────────────────────────────────────────


def test_kill_switch_rejects_every_candidate() -> None:
    decision = RiskEngine().evaluate(_candidate(), _portfolio(kill_switch=True))
    assert decision.verdict is RiskVerdict.REJECT
    assert decision.reasons == ["KILL_SWITCH"]
    assert decision.sized_qty is None


def test_kill_switch_cannot_be_overridden_by_a_permissive_context() -> None:
    ctx = RiskContext(
        regime_tradable=True,
        open_symbols=[],
        news=NewsCheck.CHECKED,
        earnings=EarningsCheck.CHECKED,
        sector="technology",
        sector_check=SectorCheck.CHECKED,
    )
    decision = RiskEngine().evaluate(_candidate(), _portfolio(kill_switch=True), context=ctx)
    assert decision.verdict is RiskVerdict.REJECT


# ── Long-only, unleveraged, no derivatives ───────────────────────────────────


def test_leverage_shorts_and_options_are_refused_at_construction() -> None:
    for flag in ("allow_leverage", "allow_short", "allow_options"):
        with pytest.raises(ValueError, match="forbids"):
            RiskEngine(RiskLimits(**{flag: True}))


def test_candidate_schema_forbids_sell_proposals() -> None:
    with pytest.raises(ValueError, match="BUY only"):
        _candidate(action=TradeAction.SELL)


def test_candidate_schema_enforces_stop_below_entry_below_target() -> None:
    with pytest.raises(ValueError, match="stop < entry < target"):
        _candidate(stop=Decimal(105))


# ── Sizing can never exceed the configured risk ──────────────────────────────


def test_position_never_risks_more_than_the_per_trade_limit() -> None:
    limits = load_risk_limits()
    decision = RiskEngine(limits).evaluate(_candidate(), _portfolio(), context=CLEARED_EARNINGS)
    assert decision.verdict is RiskVerdict.PASS
    assert decision.max_loss_usd is not None
    risked_pct = float(decision.max_loss_usd / Decimal(100000)) * 100
    assert risked_pct <= limits.max_risk_per_trade_pct + 1e-6


def test_position_never_exceeds_the_position_size_cap() -> None:
    limits = load_risk_limits()
    decision = RiskEngine(limits).evaluate(_candidate(), _portfolio(), context=CLEARED_EARNINGS)
    assert decision.sized_qty is not None
    notional = decision.sized_qty * Decimal(100)
    assert float(notional / Decimal(100000)) * 100 <= limits.max_position_pct + 1e-6


def test_loss_limits_halt_new_entries() -> None:
    engine = RiskEngine(RiskLimits(max_daily_loss_pct=2.0))
    decision = engine.evaluate(_candidate(), _portfolio(day_pnl=Decimal(-3000)))
    assert decision.verdict is RiskVerdict.REJECT
    assert "MAX_DAILY_LOSS" in decision.reasons


def test_drawdown_limit_halts_new_entries() -> None:
    engine = RiskEngine(RiskLimits(max_portfolio_drawdown_pct=10.0))
    decision = engine.evaluate(_candidate(), _portfolio(drawdown_pct=12.0))
    assert "MAX_PORTFOLIO_DRAWDOWN" in decision.reasons


def test_any_single_breach_rejects_even_when_others_pass() -> None:
    engine = RiskEngine(RiskLimits(max_open_positions=1))
    decision = engine.evaluate(_candidate(), _portfolio(open_positions=5))
    assert decision.verdict is RiskVerdict.REJECT


# ── Configuration cannot silently widen the limits ───────────────────────────


def test_locked_config_stays_within_v1_bounds() -> None:
    limits = load_risk_limits()
    assert limits.max_risk_per_trade_pct <= 1.0
    assert limits.max_position_pct <= 5.0
    assert limits.max_daily_loss_pct <= 2.0
    assert limits.max_portfolio_drawdown_pct <= 10.0
    assert not limits.allow_leverage
    assert not limits.allow_short
    assert not limits.allow_options


def test_config_file_declares_llm_cannot_trade_or_run_sql() -> None:
    config = json.loads((REPO / "configs" / "v1_paper.json").read_text())
    safety = config["safety"]
    assert safety["llm_may_place_orders"] is False
    assert safety["llm_may_run_sql"] is False
    assert safety["protective_stops_auto"] is True


# ── Live trading stays unreachable ───────────────────────────────────────────


def test_live_broker_environment_refuses_to_start() -> None:
    settings = Settings(
        TRAIDO_BROKER_ENV=BrokerEnvironment.LIVE,
        TRAIDO_ALLOW_LIVE_TRADING=False,
    )
    with pytest.raises(RuntimeError, match="Refusing to start"):
        settings.assert_safe_startup()


def test_live_trading_requires_production_then_still_blocks_v1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live needs PRODUCTION (Stage 8); V1 still refuses even when that passes."""
    settings = Settings(
        TRAIDO_BROKER_ENV=BrokerEnvironment.LIVE,
        TRAIDO_ALLOW_LIVE_TRADING=True,
    )
    with pytest.raises(RuntimeError, match="PRODUCTION"):
        settings.assert_safe_startup()

    import strategy.registry as registry

    monkeypatch.setattr(registry, "has_production_strategy", lambda: True)
    with pytest.raises(RuntimeError, match="not implemented"):
        settings.assert_safe_startup()


def test_paper_environment_starts_cleanly() -> None:
    Settings(TRAIDO_BROKER_ENV=BrokerEnvironment.PAPER).assert_safe_startup()


# ── No LLM on the capital path ───────────────────────────────────────────────


def test_risk_engine_imports_no_llm_client() -> None:
    """Risk must be deterministic code — reading its imports is the cheapest proof."""
    source = (REPO / "risk" / "risk_engine.py").read_text()
    for banned in ("anthropic", "openai", "llm", "LLMPort", "complete_json"):
        assert banned not in source, f"risk engine must not reference {banned}"


def test_no_module_lets_an_llm_execute_sql() -> None:
    for path in _python_sources():
        source = path.read_text()
        if "complete_json" not in source:
            continue
        assert "execute(" not in source, (
            f"{path.relative_to(REPO)} mixes LLM output and SQL execution in one module"
        )


_BROKER_MUTATIONS = ("place_order(", "cancel_order(")
"""The two calls on `BrokerPort` that change state at the venue.

`cancel_order` was outside this guard until the runtime-path audit, and the
omission was not harmless: `trading/reconcile.py` cancelled resting entry orders
directly for as long as the guard existed, and no test noticed. Cancelling is
not a smaller act than placing — it is what removes a protective stop.
"""


def test_broker_mutations_are_confined_to_the_broker_layer() -> None:
    """Only broker adapters, the execution service, and the port may move orders."""
    allowed_prefixes = ("broker/", "trading/execution.py", "core/ports.py")
    offenders = []
    for path in _python_sources():
        rel = str(path.relative_to(REPO))
        if rel.startswith(allowed_prefixes):
            continue
        source = path.read_text()
        for call in _BROKER_MUTATIONS:
            if call in source:
                offenders.append(f"{rel}:{call.rstrip('(')}")
    assert not offenders, f"broker mutation called outside the execution layer: {offenders}"


# ── Backtests must not flatter themselves ────────────────────────────────────


def test_default_backtest_costs_are_not_zero() -> None:
    """A default-constructed engine must charge realistic friction."""
    from quant.backtesting.engine import BacktestEngine
    from quant.backtesting.strategy import EmaTrendStub

    engine = BacktestEngine(EmaTrendStub())
    assert engine.costs.round_trip_cost_bps(Decimal(100), Decimal(100)) > 0


# ── The test suite cannot invent positions ───────────────────────────────────


def test_the_suite_never_writes_positions_into_the_journal_database() -> None:
    """A test position must never be able to appear on the desk as a holding.

    `trading.ledger.LEDGER` is a singleton that defaults to the real SQLite
    journal, so a suite without ledger isolation quietly fills the developer's
    database with open positions. The desk reads that same table and has no way
    to tell a fabricated 50 shares of AAPL from a real one — which is the whole
    problem, because every safety rule in this file is about knowing what we
    actually hold.

    The autouse `isolated_ledger` fixture in `tests/conftest.py` is what makes
    this true. This test fails the moment it stops being applied.
    """
    from trading.ledger import LEDGER

    engine = LEDGER._engine
    assert engine is not None, "the ledger is still bound to the default journal database"
    assert engine.url.database in (None, ":memory:"), (
        f"tests are writing positions to {engine.url.database!r}"
    )


# ── Kill switch durability and provenance ────────────────────────────────────


@pytest.fixture
def flag(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    """Point the kill switch at a temp flag file with Redis out of the picture."""
    import risk.kill_switch as ks

    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setattr(ks, "FLAG", tmp_path / "kill_switch.on")
    return ks


def test_kill_switch_survives_a_restart(flag) -> None:  # type: ignore[no-untyped-def]
    """State lives on disk, so a crash must not silently re-arm trading."""
    flag.set_kill_switch(True, actor="ops", reason="vendor outage")
    assert flag.FLAG.exists()
    assert flag.is_kill_switch_on() is True

    flag.set_kill_switch(False, actor="ops", reason="resolved")
    assert not flag.FLAG.exists()
    assert flag.is_kill_switch_on() is False


def test_kill_switch_records_who_halted_the_desk_and_why(flag) -> None:  # type: ignore[no-untyped-def]
    """Provenance must survive without Redis — a halt with no audit trail is a gap."""
    flag.set_kill_switch(True, actor="dmitrii", reason="fat finger")

    state = flag.get_kill_switch_state()
    assert state.enabled is True
    assert state.actor == "dmitrii"
    assert state.reason == "fat finger"
    assert state.changed_at is not None


def test_a_hand_touched_flag_file_still_halts_the_desk(flag) -> None:  # type: ignore[no-untyped-def]
    """`touch data/kill_switch.on` is a valid emergency stop, metadata or not."""
    flag.FLAG.parent.mkdir(parents=True, exist_ok=True)
    flag.FLAG.write_text("", encoding="utf-8")

    state = flag.get_kill_switch_state()
    assert state.enabled is True
    assert state.actor is None


def test_unreadable_flag_contents_still_read_as_halted(flag) -> None:  # type: ignore[no-untyped-def]
    """Corrupt metadata must never be interpreted as 'trading is fine'."""
    flag.FLAG.parent.mkdir(parents=True, exist_ok=True)
    flag.FLAG.write_text("{not json", encoding="utf-8")

    assert flag.is_kill_switch_on() is True
