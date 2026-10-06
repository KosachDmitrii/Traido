"""Independent measurements from saved facts; no vendor, order or gate calls."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.clock import ET, market_date
from core.config import get_settings
from database.models.desk import DecisionOutcomeRow, OpportunityRow, OrderIntentRow
from database.models.journal import TradeJournalRow
from database.models.monitoring import MonitoringSampleRow
from database.models.positions import OpenPositionRow
from database.models.risk_period import RiskPeriodRow
from database.session import session_factory
from risk.paper_period import PaperPeriod
from risk.risk_engine import RiskEngine
from strategy.orb import INTRADAY_VERSION, PARAMETERS, VERSION
from trading.auto_trigger_policy import get_auto_trigger_enabled
from trading.entry_policy import get_entry_thresholds
from trading.exits import OPERATOR_CLOSE_REASON
from trading.reconcile_supervisor import RECONCILE, max_reconciliation_age
from trading.session_hours import is_market_holiday, session_close, session_phase

MIN_SESSIONS = 30
MIN_TRADES = 100


@contextmanager
def observation_session(engine: Engine | None = None) -> Iterator[Session]:
    with session_factory(engine)() as db:
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            # Local to this transaction/connection; never changes trading timeouts.
            db.execute(text("SET LOCAL statement_timeout = '5s'"))
            db.execute(text("SET LOCAL lock_timeout = '1s'"))
        yield db


@lru_cache
def trading_code_hash() -> str:
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "strategy" / "orb").glob("*.py")) + [
        root / "risk" / "risk_engine.py",
        root / "risk" / "position_sizing.py",
        root / "trading" / "execution.py",
        root / "trading" / "entry_policy.py",
    ]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def utc(value: datetime) -> datetime:
    # SQLite drops offsets from timezone-aware SQL columns; they are stored in UTC.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def policy_identity() -> tuple[str, dict[str, Any]]:
    settings = get_settings()
    policy = {
        "versions": [VERSION, INTRADAY_VERSION],
        "trading_code_hash": trading_code_hash(),
        "orb_parameters": PARAMETERS,
        "risk_limits": RiskEngine().limits.model_dump(mode="json"),
        "entry_thresholds": asdict(get_entry_thresholds()),
        "exit_policy": settings.paper_exit_policy,
        "feed": settings.alpaca_data_feed,
        "broker_env": settings.broker_env.value,
        "trading_mode": settings.trading_mode.value,
        "auto_trigger_enabled": get_auto_trigger_enabled(),
    }
    encoded = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest(), policy


def record_sample(*, engine: Engine | None = None, now: datetime | None = None) -> bool:
    from agents.scanner.agent import scanner_health

    now = utc(now or datetime.now(UTC))
    minute = now.replace(second=0, microsecond=0)
    policy_hash, policy = policy_identity()
    with observation_session(engine) as db:
        if db.get(MonitoringSampleRow, minute) is not None:
            return False
        status = RECONCILE.status
        age = status.age_seconds()
        problems = []
        scanner_ok, _ = scanner_health()
        if not scanner_ok:
            problems.append("SCANNER_UNVERIFIED")
        if status.ok is not True or age is None or age > max_reconciliation_age():
            problems.append("RECONCILIATION_UNVERIFIED")
        if status.unresolved:
            problems.append("RECONCILIATION_UNRESOLVED")
        unknown = db.query(OrderIntentRow).filter(OrderIntentRow.status == "unknown").count()
        if unknown:
            problems.append("UNKNOWN_INTENTS")
        periods = db.query(RiskPeriodRow).all()
        account = None
        if len(periods) == 1:
            period = PaperPeriod.model_validate(periods[0].payload)
            source_age = (now - period.last_at).total_seconds()
            if not period.suspended and 0 <= source_age <= 180:
                account = {
                    "period_id": str(period.id),
                    "equity": str(period.last_equity),
                    "day_pnl": str(period.last_equity - period.day_baseline),
                    "drawdown_pct": period.metrics()["drawdown_pct"],
                    "source_at": period.last_at.isoformat(),
                }
        previous = (
            db.query(MonitoringSampleRow.policy_hash)
            .order_by(MonitoringSampleRow.minute.desc())
            .first()
        )
        payload = {
            "observed_at": now.isoformat(),
            "phase": session_phase(now).value,
            "policy": policy if previous is None or previous[0] != policy_hash else None,
            "problems": problems,
            "unknown_intents": unknown,
            "scanner_ok": scanner_ok,
            "reconciliation_age_seconds": age,
            "account": account,
        }
        db.add(
            MonitoringSampleRow(
                minute=minute,
                session=str(market_date(now)),
                policy_hash=policy_hash,
                payload=payload,
            )
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            # Duplicate tick after a restart/concurrent observer, not an order retry.
            if db.get(MonitoringSampleRow, minute) is None:
                raise
            return False
    return True


def completed_sessions(now: datetime, count: int = MIN_SESSIONS) -> list[date]:
    day = market_date(now)
    days: list[date] = []
    while len(days) < count:
        close = datetime.combine(day, session_close(day), ET)
        if day.weekday() < 5 and not is_market_holiday(day) and close <= now:
            days.append(day)
        day -= timedelta(days=1)
    return list(reversed(days))


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def check_entry(payload: dict[str, Any]) -> dict[str, Any]:
    """Audit the limits sealed at approval; never replace them with today's limits."""
    reasons = []
    qty = _decimal(payload.get("requested_qty"))
    price = _decimal(payload.get("limit_price"))
    risk = payload.get("risk_snapshot") or {}
    portfolio = risk.get("portfolio") or {}
    limits = risk.get("limits_applied") or {}
    equity = _decimal(portfolio.get("equity"))
    cash = _decimal(portfolio.get("cash"))
    pct = _decimal(limits.get("max_position_pct"))
    sized = _decimal(risk.get("sized_qty"))
    if (
        qty is None
        or price is None
        or equity is None
        or cash is None
        or pct is None
        or sized is None
    ):
        return {"status": "insufficient_data", "reasons": ["ENTRY_BUDGET_EVIDENCE_MISSING"]}
    if qty <= 0 or qty != qty.to_integral_value():
        reasons.append("WHOLE_SHARE_QUANTITY_VIOLATION")
    if price <= 0 or equity <= 0 or pct <= 0 or cash < 0:
        reasons.append("ENTRY_BUDGET_EVIDENCE_INVALID")
    notional = qty * price
    if notional > cash:
        reasons.append("ENTRY_EXCEEDS_SAVED_CASH")
    if notional > equity * pct / 100:
        reasons.append("ENTRY_EXCEEDS_SAVED_POSITION_CAP")
    if qty > sized:
        reasons.append("ENTRY_EXCEEDS_SAVED_RISK_SIZE")
    return {"status": "failed" if reasons else "passed", "reasons": reasons}


def _coverage(samples: list[MonitoringSampleRow], day: date) -> dict[str, Any]:
    start = datetime.combine(day, time(9, 30), ET).astimezone(UTC)
    close = datetime.combine(day, session_close(day), ET).astimezone(UTC)
    times = sorted(utc(row.minute) for row in samples if start <= utc(row.minute) <= close)
    gaps = [(b - a).total_seconds() for a, b in zip([start, *times], [*times, close], strict=True)]
    full = bool(times) and max(gaps) <= 180
    return {"complete": full, "samples": len(times), "max_gap_seconds": max(gaps)}


def build_report(*, engine: Engine | None = None, now: datetime | None = None) -> dict[str, Any]:
    now = utc(now or datetime.now(UTC))
    days = completed_sessions(now)
    start = datetime.combine(days[0], time(), ET).astimezone(UTC)
    with observation_session(engine) as db:
        samples = (
            db.query(MonitoringSampleRow)
            .filter(MonitoringSampleRow.minute >= start, MonitoringSampleRow.minute <= now)
            .order_by(MonitoringSampleRow.minute)
            .all()
        )
        latest = samples[-1] if samples else None
        # A changed configuration starts a new forward cohort. Historical facts remain visible.
        current_hash, current_policy = policy_identity()
        cohort = []
        for sample in reversed(samples):
            if sample.policy_hash != current_hash:
                break
            cohort.append(sample)
        cohort.reverse()
        cohort_start = utc(cohort[0].minute) if cohort else now
        journals = (
            db.query(TradeJournalRow)
            .filter(
                TradeJournalRow.backtest_run_id.is_(None),
                TradeJournalRow.closed_at >= start,
                TradeJournalRow.closed_at <= now,
            )
            .order_by(TradeJournalRow.closed_at, TradeJournalRow.id)
            .all()
        )
        entries = (
            db.query(OrderIntentRow)
            .filter(
                OrderIntentRow.purpose == "entry",
                OrderIntentRow.created_at >= start,
                OrderIntentRow.created_at <= now,
            )
            .all()
        )
        proposals = (
            db.query(OpportunityRow.created_at, OpportunityRow.status)
            .filter(
                OpportunityRow.created_at >= start,
                OpportunityRow.created_at <= now,
            )
            .all()
        )
        open_rows = db.query(OpenPositionRow).filter(OpenPositionRow.status == "open").all()
        outcomes = (
            db.query(
                DecisionOutcomeRow.stage,
                DecisionOutcomeRow.outcome,
                DecisionOutcomeRow.primary_reason,
                func.count(),
            )
            .filter(DecisionOutcomeRow.recorded_at >= start, DecisionOutcomeRow.recorded_at <= now)
            .group_by(
                DecisionOutcomeRow.stage,
                DecisionOutcomeRow.outcome,
                DecisionOutcomeRow.primary_reason,
            )
            .all()
        )
        duplicate_orders = (
            db.query(OrderIntentRow.broker_order_id)
            .filter(
                OrderIntentRow.broker_order_id.is_not(None),
                OrderIntentRow.created_at >= start,
                OrderIntentRow.created_at <= now,
            )
            .group_by(OrderIntentRow.broker_order_id)
            .having(func.count() > 1)
            .count()
        )
        daily = []
        for day in days:
            day_samples = [s for s in cohort if s.session == str(day)]
            health_samples = [s for s in day_samples if s.payload.get("phase") == "regular"]
            day_trades = [
                r
                for r in journals
                if r.closed_at is not None and market_date(utc(r.closed_at)) == day
            ]
            day_entries = [r for r in entries if market_date(utc(r.created_at)) == day]
            day_proposals = [r for r in proposals if market_date(utc(r.created_at)) == day]
            coverage = _coverage(day_samples, day)
            healthy = coverage["complete"] and not any(
                s.payload.get("problems") for s in health_samples
            )
            daily.append(
                {
                    "session": str(day),
                    **coverage,
                    "technical_status": "passed"
                    if healthy
                    else "failed"
                    if any(s.payload.get("problems") for s in health_samples)
                    else "insufficient_data",
                    "closed_trades": len(day_trades),
                    "unverified_trades": sum(r.pnl is None for r in day_trades),
                    "gross_closed_pnl": str(
                        sum((r.pnl for r in day_trades if r.pnl is not None), Decimal(0))
                    )
                    if day_trades
                    else "0"
                    if coverage["complete"]
                    else None,
                    "entry_intents": len(day_entries),
                    "proposals": len(day_proposals),
                    "proposal_statuses": dict(Counter(r.status for r in day_proposals)),
                    "entry_statuses": dict(Counter(r.status for r in day_entries)),
                    "budget_checks": dict(
                        Counter(check_entry(r.payload)["status"] for r in day_entries)
                    ),
                }
            )
        policies = []
        for version in [VERSION, INTRADAY_VERSION]:
            rows = [
                r
                for r in journals
                if r.strategy_version == version
                and r.closed_at is not None
                and utc(r.closed_at) >= cohort_start
                and r.opened_at is not None
                and utc(r.opened_at) >= cohort_start
                and r.pnl is not None
            ]
            values = [Decimal(r.pnl) for r in rows if r.pnl is not None]
            pnl = sum(values, Decimal(0))
            wins = [v for v in values if v > 0]
            losses = [-v for v in values if v < 0]
            policies.append(
                {
                    "strategy_version": version,
                    "closed_trades": len(rows),
                    "operator_closed_trades": sum(
                        OPERATOR_CLOSE_REASON in (r.exit_reasons or []) for r in rows
                    ),
                    "gross_closed_pnl": str(pnl),
                    "expectancy_before_costs": str(pnl / len(rows)) if rows else None,
                    "profit_factor": str(sum(wins) / sum(losses)) if losses else None,
                    "net_pnl": None,
                    "status": "insufficient_data",
                }
            )
        budget_checks = [check_entry(r.payload) for r in entries]
        budget_failures = [
            reason for c in budget_checks for reason in c["reasons"] if c["status"] == "failed"
        ]
        account = latest.payload.get("account") if latest else None
        if latest is None or (now - utc(latest.minute)).total_seconds() > 180:
            account = None
        account_samples = [s.payload["account"] for s in cohort if s.payload.get("account")]
        period_ids = {a["period_id"] for a in account_samples}
        drawdown = (
            max((a["drawdown_pct"] for a in account_samples), default=None)
            if len(period_ids) == 1
            else None
        )
        return {
            "generated_at": now.isoformat(),
            "last_sample_at": latest.minute.isoformat() if latest else None,
            "sample_age_seconds": (now - utc(latest.minute)).total_seconds() if latest else None,
            "policy_hash": current_hash,
            "policy": current_policy,
            "cohort_started_at": cohort_start.isoformat() if cohort else None,
            "criteria": {
                "min_sessions": MIN_SESSIONS,
                "min_closed_trades_per_version": MIN_TRADES,
                "max_observation_gap_seconds": 180,
                "checkpoints_are_not_profitability_proof": True,
            },
            "complete_sessions": sum(d["complete"] for d in daily),
            "technical_status": "failed"
            if duplicate_orders
            or any(s.payload.get("problems") for s in cohort if s.payload.get("phase") == "regular")
            else "passed"
            if all(d["technical_status"] == "passed" for d in daily)
            else "insufficient_data",
            "duplicate_broker_order_ids": duplicate_orders,
            "proposals": len(proposals),
            "proposal_statuses": dict(Counter(r.status for r in proposals)),
            "entry_statuses": dict(Counter(r.status for r in entries)),
            "latest_problems": latest.payload.get("problems", []) if latest else [],
            "budget_status": "failed" if budget_failures else "insufficient_data",
            "budget_checks": dict(Counter(c["status"] for c in budget_checks)),
            "budget_failures": dict(Counter(budget_failures)),
            "budget_missing_evidence": [
                "AGGREGATE_PENDING_RESERVATIONS_NOT_PROVEN",
                "SMALL_ACCOUNT_SCENARIOS_NOT_PROVEN",
            ],
            "strategy_status": "insufficient_data",
            "strategies": policies,
            "account": account,
            "sampled_account_drawdown_pct": drawdown,
            "open_positions": [
                {"symbol": r.symbol, "qty": str(r.qty), "strategy_version": r.strategy_version}
                for r in open_rows
            ],
            "missing_evidence": [
                "FEES_AND_SUBSCRIPTIONS_NOT_RECORDED",
                "HISTORICAL_OPEN_POSITION_VALUATIONS_MISSING",
                "EXTERNAL_CASH_FLOWS_NOT_VERIFIED",
                "HELD_OUT_BACKTEST_MISSING",
            ],
            "daily": daily,
            "funnel": [
                {"stage": stage, "outcome": outcome, "reason": reason, "count": count}
                for stage, outcome, reason, count in outcomes
            ],
            "funnel_counts_are_observations_not_unique_signals": True,
            "live_ready": False,
        }


def refresh_report(*, engine: Engine | None = None, now: datetime | None = None) -> None:
    """Only observer-owned reports are updated; all trading evidence stays untouched."""
    from database.models.monitoring import MonitoringReportRow

    now = utc(now or datetime.now(UTC))
    report = build_report(engine=engine, now=now)
    with observation_session(engine) as db:
        key = str(market_date(now))
        row = db.get(MonitoringReportRow, key)
        if row is None:
            db.add(MonitoringReportRow(session=key, payload=report))
        else:
            row.payload = report
        db.commit()


def saved_report(*, engine: Engine | None = None, now: datetime | None = None) -> dict[str, Any]:
    from database.models.monitoring import MonitoringReportRow

    now = utc(now or datetime.now(UTC))
    with observation_session(engine) as db:
        row = db.query(MonitoringReportRow).order_by(MonitoringReportRow.session.desc()).first()
        if row is None:
            return {"available": False, "stale": True}
        result = dict(row.payload)
    age = (now - datetime.fromisoformat(result["generated_at"])).total_seconds()
    stale = not 0 <= age <= 600
    return {**result, "available": True, "stale": stale, "report_age_seconds": age}
