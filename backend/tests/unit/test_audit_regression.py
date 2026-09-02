"""Audit regression suite — capital-path fail-closed invariants."""

from __future__ import annotations

import json
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from core.enums import (
    AdmissionDecision,
    DataHealthStatus,
    InstrumentThesis,
    MarketRegimeLabel,
    SetupType,
    TargetReachabilityClass,
    Timeframe,
    TradeAction,
)
from core.schemas import (
    AdmissionSnapshot,
    EntryTimingFacts,
    FeatureSnapshot,
    MarketAssessment,
    Quote,
    TargetPlan,
    TradeCandidate,
)
from trading.admission_records import AdmissionIdempotencyConflict, persist_admission
from trading.data_integrity import check_data_integrity
from trading.effective_rr import compute_effective_rr
from trading.final_pretrade import PretradeRejection, final_pretrade_validation
from trading.market_gate import evaluate_market_gate, evaluate_market_gate_for_candidate
from trading.stop_validation import validate_stop
from trading.target_validation import validate_target
from trading.watch_enrichment import desk_payload, refresh_watch_desk_cache


def _snap(close: float = 100.0, atr: float = 2.0) -> FeatureSnapshot:
    return FeatureSnapshot(
        symbol="TEST",
        timeframe=Timeframe.H1,
        computed_at=datetime.now(UTC),
        indicators={"close": close, "atr_14": atr, "sma_20": close - 1, "vwap": close - 0.5},
        candlestick_patterns={},
        chart_patterns={},
        support=[Decimal(str(close - 5))],
        resistance=[Decimal(str(close + 10))],
    )


def _candidate(**kw) -> TradeCandidate:
    base = {
        "symbol": "TEST",
        "action": TradeAction.BUY,
        "confidence": 0.8,
        "entry": Decimal(100),
        "stop": Decimal(95),
        "target": Decimal(115),
        "risk_reward": 3.0,
        "reasons": ["test"],
        "strategy_version": "test@1",
        "thesis": InstrumentThesis.BULLISH,
        "setup_type": SetupType.PULLBACK_CONTINUATION,
        "setup_quality": 80,
        "entry_quality": 75,
        "entry_zone_low": Decimal(99),
        "entry_zone_high": Decimal(101),
        "admission_version": "admission@1.1.0",
        "admission_snapshot": AdmissionSnapshot(
            price_at_creation=100.0,
            atr_at_creation=2.0,
            setup_type=SetupType.PULLBACK_CONTINUATION,
            entry_zone_low=99.0,
            entry_zone_high=101.0,
            setup_quality_at_creation=80,
            entry_quality_at_creation=75,
            stop_at_creation=95.0,
            target_at_creation=115.0,
        ).model_dump(mode="json"),
    }
    base.update(kw)
    return TradeCandidate(**base)


def _quote(*, age_sec: float = 0.0, bid: float = 99.95, ask: float = 100.05) -> Quote:
    return Quote(
        symbol="TEST",
        bid=Decimal(str(bid)),
        ask=Decimal(str(ask)),
        ts=datetime.now(UTC) - timedelta(seconds=age_sec),
        source="test",
    )


def test_day_old_quote_data_blocked() -> None:
    now = datetime.now(UTC)
    with pytest.raises(PretradeRejection) as exc:
        final_pretrade_validation(
            _candidate(),
            quote=_quote(age_sec=86400),
            bars_count=60,
            last_bar_ts=now,
            now=now,
            exec_snap=_snap(),
        )
    assert exc.value.code == "BUY_REJECTED_STALE_DATA"


def test_quote_without_usable_timestamp_blocked() -> None:
    # Naive timestamp → DATA_BLOCKED
    q = Quote(
        symbol="TEST",
        bid=Decimal("99.95"),
        ask=Decimal("100.05"),
        ts=datetime.utcnow(),  # noqa: DTZ003 — intentional naive
        source="test",
    )
    data = check_data_integrity(quote=q, now=datetime.now(UTC), require_bars=False)
    assert data.status is DataHealthStatus.UNHEALTHY
    assert "QUOTE_TIMESTAMP_INVALID" in data.reason_codes


def test_missing_bars_data_blocked() -> None:
    now = datetime.now(UTC)
    with pytest.raises(PretradeRejection) as exc:
        final_pretrade_validation(
            _candidate(),
            quote=_quote(),
            bars_count=None,
            last_bar_ts=None,
            now=now,
            exec_snap=_snap(),
        )
    assert "BARS_REQUIRED" in exc.value.detail


def test_stale_bars_data_blocked() -> None:
    now = datetime.now(UTC)
    data = check_data_integrity(
        quote=_quote(),
        bars_count=60,
        last_bar_ts=now - timedelta(days=2),
        now=now,
        require_bars=True,
    )
    assert data.status is DataHealthStatus.UNHEALTHY
    assert "STALE_BARS" in data.reason_codes


def test_market_regime_missing_blocked() -> None:
    gate = evaluate_market_gate(None)
    assert gate.status is DataHealthStatus.UNHEALTHY
    assert "REGIME_MISSING" in gate.reason_codes


def test_market_regime_unknown_blocked() -> None:
    cand = _candidate(market_label="nonsense")
    gate = evaluate_market_gate_for_candidate(cand)
    assert gate.status is DataHealthStatus.UNHEALTHY
    assert any("REGIME_UNKNOWN" in r for r in gate.reason_codes)


def test_market_regime_stale_blocked() -> None:
    market = MarketAssessment(
        regime=MarketRegimeLabel.NEUTRAL,
        score=50,
        risk_posture="neutral",
        evaluated_at=datetime.now(UTC) - timedelta(minutes=30),
        benchmark="SPY",
        sector_label="tech",
        sector_tradable=True,
    )
    gate = evaluate_market_gate(market, now=datetime.now(UTC), require_sector=True)
    assert gate.status is DataHealthStatus.UNHEALTHY
    assert "REGIME_STALE" in gate.reason_codes


def test_sector_gate_missing_blocked() -> None:
    market = MarketAssessment(
        regime=MarketRegimeLabel.NEUTRAL,
        score=50,
        risk_posture="neutral",
        evaluated_at=datetime.now(UTC),
        benchmark="SPY",
    )
    gate = evaluate_market_gate(market, now=datetime.now(UTC), require_sector=True)
    assert gate.status is DataHealthStatus.UNHEALTHY
    assert "SECTOR_GATE_MISSING" in gate.reason_codes


def test_target_plan_mismatch() -> None:
    plan = TargetPlan(
        price=Decimal(120),
        model="structure",
        reachability=TargetReachabilityClass.REALISTIC,
    )
    res = validate_target(entry=100, target=115, target_plan=plan)
    assert not res.valid
    assert "TARGET_PLAN_MISMATCH" in res.reason_codes


def test_atr_only_stop_not_structural() -> None:
    facts = EntryTimingFacts(current_price=100.0, atr=2.0, stop_distance_atr=2.0)
    res = validate_stop(entry=100, stop=96, facts=facts, stop_model="atr")
    assert res.structural_basis is False
    assert "ATR_ONLY_STOP" in res.reason_codes


def test_effective_rr_below_floor() -> None:
    q = _quote(bid=99.9, ask=100.1)
    rr = compute_effective_rr(entry=100.1, stop=99.5, target=101.0, quote=q)
    assert rr.effective_rr < 2.0


def test_idempotency_conflict_different_payload() -> None:
    from core.schemas import TradeAdmissionResult

    adm = TradeAdmissionResult(
        decision=AdmissionDecision.BUY_ALLOWED,
        admitted=True,
        setup_quality=80,
        entry_quality=75,
    )
    oid = uuid4()
    persist_admission(
        symbol="TEST",
        admission=adm,
        opportunity_id=oid,
        geometry_hash="abc123",
        phase="approval",
        trigger_version=1,
        context={"v": 1},
    )
    adm2 = adm.model_copy(update={"setup_quality": 99})
    with pytest.raises(AdmissionIdempotencyConflict):
        persist_admission(
            symbol="TEST",
            admission=adm2,
            opportunity_id=oid,
            geometry_hash="abc123",
            phase="approval",
            trigger_version=1,
            context={"v": 2},
        )


@pytest.mark.asyncio
async def test_desk_enrichment_not_recursive() -> None:
    from core.enums import EntryWatchStatus
    from core.schemas import EntryWatch

    watch = EntryWatch(
        id=uuid4(),
        symbol="TEST",
        strategy_version="test",
        created_at=datetime.now(UTC),
        valid_until=datetime.now(UTC) + timedelta(hours=1),
        thesis=InstrumentThesis.BULLISH,
        signal_price=Decimal(100),
        current_price_at_creation=Decimal(100),
        entry_zone_low=Decimal(99),
        entry_zone_high=Decimal(101),
        planned_entry=Decimal(100),
        planned_stop=Decimal(95),
        planned_target=Decimal(115),
        entry_quality_at_creation=70,
        status=EntryWatchStatus.WAITING,
    )
    sizes: list[int] = []
    current = watch
    for _ in range(20):
        current = await refresh_watch_desk_cache(current, price=100.0, quote=None, md=None)
        blob = json.dumps(current.desk_enrichment)
        sizes.append(len(blob))
        assert "desk_enrichment" not in current.desk_enrichment
    # Payload size stays roughly constant (no recursive nesting).
    assert max(sizes) < sizes[0] * 2 + 500


def test_desk_payload_preserves_machine_status() -> None:
    from core.enums import EntryWatchStatus
    from core.schemas import EntryWatch

    watch = EntryWatch(
        id=uuid4(),
        symbol="TEST",
        strategy_version="test",
        created_at=datetime.now(UTC),
        valid_until=datetime.now(UTC) + timedelta(hours=1),
        thesis=InstrumentThesis.BULLISH,
        signal_price=Decimal(100),
        current_price_at_creation=Decimal(100),
        entry_zone_low=Decimal(99),
        entry_zone_high=Decimal(101),
        planned_entry=Decimal(100),
        planned_stop=Decimal(95),
        planned_target=Decimal(115),
        entry_quality_at_creation=70,
        status=EntryWatchStatus.ADMITTED,
        desk_enrichment={"status": "WAITING", "ui_state": "WAITING"},
    )
    payload = desk_payload(watch)
    assert payload["status"] == "admitted"


def test_export_archive_excludes_forbidden(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    script = root / "backend" / "scripts" / "export_review_archive.py"
    if not script.exists():
        script = root / "backend" / "scripts" / "export_review_archive.sh"
    out = tmp_path / "review.zip"
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, str(script), "-o", str(out)],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    assert out.exists(), proc.stderr
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    forbidden = []
    for n in names:
        norm = n.replace("\\", "/")
        if (
            norm == ".env"
            or norm.endswith(("/.env", "traido_journal.db"))
            or "/.git/" in f"/{norm}"
            or norm.startswith(".git/")
            or "/node_modules/" in f"/{norm}"
            or "/.venv/" in f"/{norm}"
            or "/.venv312/" in f"/{norm}"
            or "/dist/" in f"/{norm}"
            or "/backend/data/backups/" in f"/{norm}"
        ):
            forbidden.append(norm)
    assert forbidden == [], f"forbidden paths in archive: {forbidden}"
