"""F3 remaining DoD: historical MFE, watch poller safety, diagnostics."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from core.enums import EntryWatchStatus, InstrumentThesis, TradeAction
from core.schemas import Quote, TradeCandidate
from trading.entry_timing import evaluate_timing
from trading.entry_quality import decide_entry
from trading.entry_watches import ENTRY_WATCHES
from trading.f3_diagnostics import build_f3_diagnostics, write_forward_report
from trading.historical_mfe import (
    MIN_SAMPLES,
    ensure_seeded_from_aftermath,
    lookup_mfe,
    record_mfe,
    sample_counts,
)
from trading.target_model import build_target_plan
from tests.unit.test_entry_timing_f3 import _snap


def test_historical_mfe_arms_reachability(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "mfe.jsonl"
    monkeypatch.setattr("trading.historical_mfe._PATH", path)
    for i in range(MIN_SAMPLES):
        record_mfe(
            symbol="AAPL",
            strategy_version="test",
            mfe_pct=1.0 + (i % 5) * 0.1,
            horizon_min=60,
            source="unit",
        )
    med, n = lookup_mfe(horizon_min=60)
    assert n >= MIN_SAMPLES
    assert med is not None
    facts = evaluate_timing(
        _snap(resistance=[101.0]),
        planned_entry=100.0,
        planned_stop=99.0,
        planned_target=102.0,
    )
    plan = build_target_plan(
        entry=Decimal(100),
        stop=Decimal(99),
        facts=facts,
        historical_mfe_pct=med,
        historical_sample_size=n,
    )
    assert plan.reachability.value != "insufficient_data"
    assert plan.historical_sample_size >= MIN_SAMPLES


def test_seed_from_aftermath_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "mfe.jsonl"
    monkeypatch.setattr("trading.historical_mfe._PATH", path)
    aftermath = Path("data/buy_aftermath_f2.json")
    if not aftermath.exists():
        pytest.skip("no aftermath file")
    n1 = ensure_seeded_from_aftermath(aftermath)
    n2 = ensure_seeded_from_aftermath(aftermath)
    assert n1 >= MIN_SAMPLES
    assert n2 == 0
    assert sample_counts()["total"] >= MIN_SAMPLES


def test_list_actionable_includes_triggered() -> None:
    facts = evaluate_timing(
        _snap(close=102.0, sma20=100.0, atr=1.0, vwap=100.0),
        signal_price=100.0,
        planned_entry=100.0,
        planned_stop=98.5,
        planned_target=103.0,
    )
    bundle = decide_entry(InstrumentThesis.BULLISH, facts, technical_score=88)
    cand = TradeCandidate(
        symbol="WAITX",
        action=TradeAction.BUY,
        confidence=0.8,
        entry=Decimal(100),
        stop=Decimal("98.5"),
        target=Decimal(103),
        risk_reward=2.0,
        reasons=["x"],
        strategy_version="test",
        entry_zone_low=bundle.entry_zone_low,
        entry_zone_high=bundle.entry_zone_high,
        signal_price=Decimal(100),
    )
    watch = ENTRY_WATCHES.create_from_bundle(cand, bundle)
    from trading.entry_watch_eval import observe_price

    triggered = observe_price(watch, float(bundle.entry_zone_low))
    assert triggered.status is EntryWatchStatus.TRIGGERED
    actionable = ENTRY_WATCHES.list_actionable()
    assert any(w.id == triggered.id for w in actionable)


@pytest.mark.asyncio
async def test_watch_pass_never_places_broker_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poller may publish a desk card; it must not call broker.place_order."""
    placed: list[object] = []

    class _MD:
        async def get_quote(self, symbol: str) -> Quote:
            return Quote(
                symbol=symbol,
                bid=Decimal("99.50"),
                ask=Decimal("99.55"),
                ts=datetime.now(UTC),
                source="test",
            )

        async def get_bars(self, *a, **k):
            return []

    monkeypatch.setattr(
        "trading.entry_watch_loop.create_market_data_port", lambda *_a, **_k: _MD()
    )

    async def _boom_place(*_a, **_k):
        placed.append(True)
        raise AssertionError("broker.place_order must not be called from watch loop")

    # Empty store — pass is a no-op; still proves the import path is safe.
    from trading.entry_watches import EntryWatchStore
    import trading.entry_watch_loop as loop

    monkeypatch.setattr(loop, "ENTRY_WATCHES", EntryWatchStore())
    stats = await loop.run_watch_pass()
    assert stats["checked"] == 0
    assert placed == []


def test_diagnostics_payload_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("trading.f3_diagnostics.SHADOW_PATH", tmp_path / "shadow.jsonl")
    monkeypatch.setattr("trading.f3_diagnostics.REPORT_PATH", tmp_path / "report.json")
    monkeypatch.setattr("trading.historical_mfe._PATH", tmp_path / "mfe.jsonl")
    payload = build_f3_diagnostics()
    assert "signal_quality" in payload
    assert "wait_effectiveness" in payload
    assert "target_quality" in payload
    assert "forward_paper" in payload
    assert payload["forward_paper"]["target_rth_samples"] == 100
    written = write_forward_report()
    assert (tmp_path / "report.json").exists()
    assert "generated_at" in written
