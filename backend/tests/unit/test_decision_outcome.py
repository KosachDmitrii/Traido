"""DecisionOutcome persists and is readable after the in-memory ledger is gone."""

from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.enums import AdmissionDecision, RiskVerdict
from database.base import Base
from trading.decision_outcome import DecisionOutcomeLedger


def _bind_journal(tmp_path, monkeypatch):
    import database.models  # noqa: F401 — register metadata

    engine = create_engine(f"sqlite:///{tmp_path / 'outcomes.db'}", future=True)
    Base.metadata.create_all(engine)

    def factory(_engine=None):
        return sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    monkeypatch.setattr("database.session.session_factory", factory)
    return engine


def test_list_for_symbol_reads_persisted_rows_after_restart(tmp_path, monkeypatch) -> None:
    _bind_journal(tmp_path, monkeypatch)
    live = DecisionOutcomeLedger()
    live.record(
        symbol="aapl",
        stage="pre_watch",
        outcome="DATA_BLOCKED",
        primary_reason="NEWS_NOT_CONFIGURED",
        reason_codes=("NEWS_NOT_CONFIGURED",),
        admission=AdmissionDecision.WAIT,
        risk_verdict=RiskVerdict.REJECT,
    )

    restarted = DecisionOutcomeLedger()
    rows = restarted.list_for_symbol("AAPL")
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    assert rows[0].outcome == "DATA_BLOCKED"
    assert rows[0].primary_reason == "NEWS_NOT_CONFIGURED"
    assert rows[0].reason_codes == ("NEWS_NOT_CONFIGURED",)
    assert rows[0].admission is AdmissionDecision.WAIT
    assert rows[0].risk_verdict is RiskVerdict.REJECT


def test_summary_reads_persisted_counts_after_restart(tmp_path, monkeypatch) -> None:
    _bind_journal(tmp_path, monkeypatch)
    live = DecisionOutcomeLedger()
    live.record(symbol="MSFT", stage="admission", outcome="WAIT", primary_reason="WAITING")
    live.record(symbol="MSFT", stage="admission", outcome="NO_TRADE", primary_reason="REGIME")

    restarted = DecisionOutcomeLedger()
    counts = restarted.summary()
    assert counts["admission:WAIT"] == 1
    assert counts["admission:NO_TRADE"] == 1


def test_list_for_symbol_falls_back_to_memory_when_db_unreadable(monkeypatch) -> None:
    def boom(_engine=None):
        raise RuntimeError("journal down")

    monkeypatch.setattr("database.session.session_factory", boom)
    ledger = DecisionOutcomeLedger()
    ledger.record(symbol="NVDA", stage="scan", outcome="WAIT", primary_reason="PRE_WATCH_ELIGIBLE")
    rows = ledger.list_for_symbol("NVDA")
    assert len(rows) == 1
    assert rows[0].primary_reason == "PRE_WATCH_ELIGIBLE"
