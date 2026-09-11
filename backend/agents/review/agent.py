"""Review Agent — journal analytics only; no trading authority."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from core.activity import BOARD
from database.models.journal import TradeJournalRow
from database.session import session_factory

REVIEW_VERSION = "review@0.1.0"


@dataclass
class ReviewReport:
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    expectancy: float | None
    profit_factor: float | None
    avg_pnl: float | None
    avg_pnl_pct: float | None
    by_strategy: list[dict[str, Any]]
    by_symbol: list[dict[str, Any]]
    recent: list[dict[str, Any]]
    notes: list[str]
    version: str = REVIEW_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "trade_count": self.trade_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "win_rate": self.win_rate,
            "expectancy": self.expectancy,
            "profit_factor": self.profit_factor,
            "avg_pnl": self.avg_pnl,
            "avg_pnl_pct": self.avg_pnl_pct,
            "by_strategy": self.by_strategy,
            "by_symbol": self.by_symbol,
            "recent": self.recent,
            "notes": self.notes,
            "version": self.version,
        }


def _row_to_dict(row: TradeJournalRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "symbol": row.symbol,
        "entry": str(row.entry),
        "exit": str(row.exit) if row.exit is not None else None,
        "qty": str(row.qty),
        "pnl": str(row.pnl) if row.pnl is not None else None,
        "exit_price_status": "verified" if row.pnl is not None else "unverified",
        "pnl_pct": row.pnl_pct,
        "strategy_version": row.strategy_version,
        "entry_reasons": row.entry_reasons or [],
        "exit_reasons": row.exit_reasons or [],
        "opened_at": row.opened_at.isoformat() if row.opened_at else None,
        "closed_at": row.closed_at.isoformat() if row.closed_at else None,
        "risk_reward_planned": row.risk_reward_planned,
    }


def _bucket_stats(rows: list[TradeJournalRow]) -> dict[str, Any]:
    if not rows:
        return {
            "trade_count": 0,
            "win_count": 0,
            "loss_count": 0,
            "win_rate": 0.0,
            "avg_pnl": None,
            "avg_pnl_pct": None,
        }
    wins = [r for r in rows if Decimal(str(r.pnl)) > 0]
    losses = [r for r in rows if Decimal(str(r.pnl)) <= 0]
    pnls = [float(Decimal(str(r.pnl))) for r in rows]
    pcts = [float(r.pnl_pct) for r in rows if r.pnl_pct is not None]
    return {
        "trade_count": len(rows),
        "win_count": len(wins),
        "loss_count": len(losses),
        "win_rate": len(wins) / len(rows) if rows else 0.0,
        "avg_pnl": sum(pnls) / len(pnls),
        "avg_pnl_pct": sum(pcts) / len(pcts),
    }


def build_review(
    *,
    live_only: bool = True,
    limit: int = 200,
    engine=None,
    announce: bool = True,
) -> ReviewReport:
    if announce:
        # `announce=False` means "a poll is reading the journal", not "the review
        # agent is running". The desk rebuilds this report on every poll, so an
        # unconditional `working` here re-stamped the activity window several
        # times a minute and left Review animating as though it never stopped.
        # The terminal write below is not gated: it carries the trade count and
        # win rate the panel shows at rest, and it does not touch the window.
        BOARD.set_agent("review", status="working", detail="Aggregating journal")
    SessionLocal = session_factory(engine)
    with SessionLocal() as session:
        q = session.query(TradeJournalRow)
        if live_only:
            q = q.filter(TradeJournalRow.backtest_run_id.is_(None))
        rows = list(q.order_by(TradeJournalRow.closed_at.desc().nullslast()).limit(limit).all())

    unverified = sum(row.pnl is None or row.pnl_pct is None for row in rows)
    rows = [row for row in rows if row.pnl is not None and row.pnl_pct is not None]

    if not rows:
        report = ReviewReport(
            trade_count=0,
            win_count=0,
            loss_count=0,
            win_rate=0.0,
            expectancy=None,
            profit_factor=None,
            avg_pnl=None,
            avg_pnl_pct=None,
            by_strategy=[],
            by_symbol=[],
            recent=[],
            notes=[
                f"{unverified} closed trades await verified exit prices; excluded from statistics."
                if unverified
                else "No closed paper trades yet — approve and exit to build the journal."
            ],
        )
        BOARD.set_agent("review", status="idle", detail="No trades yet", score=0)
        return report

    wins = [r for r in rows if Decimal(str(r.pnl)) > 0]
    losses = [r for r in rows if Decimal(str(r.pnl)) <= 0]
    gross_win = sum((Decimal(str(r.pnl)) for r in wins), Decimal(0))
    gross_loss = abs(sum((Decimal(str(r.pnl)) for r in losses), Decimal(0)))
    pnls = [float(Decimal(str(r.pnl))) for r in rows]
    pcts = [float(r.pnl_pct) for r in rows if r.pnl_pct is not None]
    expectancy = sum(pnls) / len(pnls)
    profit_factor = float(gross_win / gross_loss) if gross_loss > 0 else None

    by_strat: dict[str, list[TradeJournalRow]] = defaultdict(list)
    by_sym: dict[str, list[TradeJournalRow]] = defaultdict(list)
    for r in rows:
        by_strat[r.strategy_version or "unknown"].append(r)
        by_sym[r.symbol].append(r)

    notes: list[str] = []
    if unverified:
        notes.append(
            f"{unverified} closed trades await verified exit prices; excluded from statistics."
        )
    stop_exits = sum(
        1 for r in rows if any("stop" in (x or "").lower() for x in (r.exit_reasons or []))
    )
    target_exits = sum(
        1 for r in rows if any("target" in (x or "").lower() for x in (r.exit_reasons or []))
    )
    if stop_exits:
        notes.append(f"{stop_exits}/{len(rows)} closed near stop — review entry timing")
    if target_exits:
        notes.append(f"{target_exits}/{len(rows)} hit target — strategy geometry working")
    if expectancy is not None:
        notes.append(
            f"Expectancy ${expectancy:.2f}/trade · win rate {len(wins) / len(rows) * 100:.0f}%"
        )
    best = max(by_sym.items(), key=lambda kv: _bucket_stats(kv[1])["avg_pnl"] or 0)
    worst = min(by_sym.items(), key=lambda kv: _bucket_stats(kv[1])["avg_pnl"] or 0)
    if best[0] != worst[0]:
        notes.append(f"Best symbol {best[0]} · weakest {worst[0]}")

    report = ReviewReport(
        trade_count=len(rows),
        win_count=len(wins),
        loss_count=len(losses),
        win_rate=len(wins) / len(rows),
        expectancy=expectancy,
        profit_factor=profit_factor,
        avg_pnl=sum(pnls) / len(pnls),
        avg_pnl_pct=sum(pcts) / len(pcts),
        by_strategy=[
            {"strategy_version": k, **_bucket_stats(v)} for k, v in sorted(by_strat.items())
        ],
        by_symbol=[{"symbol": k, **_bucket_stats(v)} for k, v in sorted(by_sym.items())],
        recent=[_row_to_dict(r) for r in rows[:15]],
        notes=notes,
    )
    BOARD.set_agent(
        "review",
        status="done",
        detail=f"{report.trade_count} trades · WR {report.win_rate:.0%}",
        score=int(report.win_rate * 100),
    )
    if announce:
        BOARD.log(
            "review",
            f"Journal: {report.trade_count} trades · expectancy {report.expectancy}",
        )
    return report


def journal_page(*, page: int = 1, page_size: int = 10, engine=None) -> dict[str, Any]:
    """Read a bounded page of actual trades, excluding backtest rows."""
    SessionLocal = session_factory(engine)
    with SessionLocal() as session:
        query = session.query(TradeJournalRow).filter(TradeJournalRow.backtest_run_id.is_(None))
        total = query.count()
        page_count = max(1, (total + page_size - 1) // page_size)
        page = min(max(1, page), page_count)
        rows = (
            query.order_by(TradeJournalRow.closed_at.desc().nullslast(), TradeJournalRow.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
            .all()
        )
        return {
            "items": [_row_to_dict(row) for row in rows],
            "total": total,
            "unverified_count": query.filter(TradeJournalRow.pnl.is_(None)).count(),
            "page": page,
            "page_size": page_size,
            "page_count": page_count,
        }
