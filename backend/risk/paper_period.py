"""Explicit IBKR Paper observation epoch, NOT reconstructed account history.

Measures sampled net-liquidation changes. External funding/reset is unsupported:
the operator must suspend the period BEFORE changing the paper balance. There
is deliberately no reset/resume endpoint that could erase an accumulated loss.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import RLock
from typing import Literal
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import update

from core.clock import ET
from database.models.desk import AuditEventRow
from database.models.risk_period import RiskPeriodRow
from database.session import session_factory

_LOCK = RLock()


class RiskPeriodError(ValueError):
    pass


class PaperPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    id: UUID
    account_id: str = Field(min_length=1)
    currency: str
    started_at: AwareDatetime
    initial_equity: Decimal = Field(gt=0)
    last_at: AwareDatetime
    last_equity: Decimal = Field(gt=0)
    high_water: Decimal = Field(gt=0)
    week_key: str
    week_baseline_at: AwareDatetime
    week_baseline: Decimal = Field(gt=0)
    day_key: str
    day_baseline: Decimal = Field(gt=0)
    suspended: bool
    source: Literal["ibkr_paper_observed_net_liquidation_v1"]

    @model_validator(mode="after")
    def consistent(self):
        if self.high_water < max(
            self.initial_equity, self.last_equity, self.week_baseline, self.day_baseline
        ):
            raise ValueError("RISK_HIGH_WATER_INVALID")
        if not self.started_at <= self.week_baseline_at <= self.last_at:
            raise ValueError("RISK_PERIOD_TIMELINE_INVALID")
        _key(self.account_id, self.currency)
        return self

    def metrics(self) -> dict:
        return {
            "risk_period_id": str(self.id),
            "risk_period_started_at": self.started_at,
            "risk_history_status": "suspended" if self.suspended else "observed_period",
            "risk_history_source": self.source,
            "risk_observed_at": self.last_at,
            "risk_week_baseline_at": self.week_baseline_at,
            "risk_week_baseline_equity": self.week_baseline,
            # With an active observed epoch, daily risk must not keep using
            # the adapter's realized-only fallback. Include open P&L too.
            **(
                {"day_pnl": self.last_equity - self.day_baseline, "day_pnl_source": self.source}
                if not self.suspended
                else {}
            ),
            "week_pnl": None if self.suspended else self.last_equity - self.week_baseline,
            "drawdown_pct": None
            if self.suspended
            else float(
                max(Decimal(0), (self.high_water - self.last_equity) / self.high_water * 100)
            ),
        }


def _key(account: str, currency: str) -> str:
    if not account or not account.startswith("DU") or currency != "USD":
        raise RiskPeriodError("IBKR_PAPER_USD_ACCOUNT_REQUIRED")
    return f"ibkr:paper:{account}:{currency}"


def _week(now: datetime) -> str:
    local = now.astimezone(ET).date()
    return (local - timedelta(days=local.weekday())).isoformat()


def _validate_equity(equity: Decimal, now: datetime) -> None:
    if not equity.is_finite() or equity <= 0:
        raise RiskPeriodError("RISK_EQUITY_INVALID")
    if now.tzinfo is None or now.utcoffset() is None:
        raise RiskPeriodError("RISK_TIMESTAMP_INVALID")


def _audit(session, event: str, period: PaperPeriod, actor: str) -> None:
    session.add(
        AuditEventRow(
            event_type=event,
            actor=actor,
            created_at=datetime.now(UTC),
            payload=period.model_dump(mode="json"),
        )
    )


def start_period(account: str, currency: str, equity: Decimal, now: datetime) -> PaperPeriod:
    """Operator-only. Repeating a request never resets a baseline or high water."""
    key = _key(account, currency)
    _validate_equity(equity, now)
    with _LOCK, session_factory()() as session:
        row = session.get(RiskPeriodRow, key)
        if row is not None:
            existing = PaperPeriod.model_validate(row.payload)
            if existing.suspended:
                raise RiskPeriodError("RISK_PERIOD_SUSPENDED_RECONCILIATION_REQUIRED")
            return existing
        period = PaperPeriod(
            id=uuid4(),
            suspended=False,
            source="ibkr_paper_observed_net_liquidation_v1",
            account_id=account,
            currency=currency,
            started_at=now,
            initial_equity=equity,
            last_at=now,
            last_equity=equity,
            high_water=equity,
            week_key=_week(now),
            week_baseline_at=now,
            week_baseline=equity,
            day_key=now.astimezone(ET).date().isoformat(),
            day_baseline=equity,
        )
        session.add(
            RiskPeriodRow(account_key=key, version=0, payload=period.model_dump(mode="json"))
        )
        _audit(session, "PaperRiskPeriodStarted", period, "user")
        session.commit()
        return period


def observe(account: str, currency: str, equity: Decimal, now: datetime) -> PaperPeriod | None:
    key = _key(account, currency)
    _validate_equity(equity, now)
    with _LOCK, session_factory()() as session:
        row = session.get(RiskPeriodRow, key)
        if row is None:
            return None  # Polling cannot consent to a new period.
        period = PaperPeriod.model_validate(row.payload)
        if period.account_id != account or period.currency != currency:
            raise RiskPeriodError("RISK_ACCOUNT_MISMATCH")
        if now < period.last_at:
            raise RiskPeriodError("RISK_OBSERVATION_OUT_OF_ORDER")
        if period.suspended:
            return period
        day = now.astimezone(ET).date().isoformat()
        if (
            equity == period.last_equity
            and day == period.day_key
            and _week(now) == period.week_key
            and now - period.last_at < timedelta(minutes=1)
        ):
            return period  # Do not amplify each scanner candidate into a DB write.
        if day != period.day_key:
            period.day_key = day
            period.day_baseline = period.last_equity
        if _week(now) != period.week_key:
            # Carry the last observed value across the boundary: the first
            # Monday read must not erase a loss incurred while disconnected.
            # Timestamp discloses any gap; this is not a claimed Monday quote.
            period.week_key = _week(now)
            period.week_baseline = period.last_equity
            period.week_baseline_at = period.last_at
        period.high_water = max(period.high_water, equity)
        period.last_equity = equity
        period.last_at = now
        changed = session.execute(
            update(RiskPeriodRow)
            .where(
                RiskPeriodRow.account_key == key,
                RiskPeriodRow.version == row.version,
            )
            .values(version=row.version + 1, payload=period.model_dump(mode="json"))
        )
        if changed.rowcount != 1:
            raise RiskPeriodError("RISK_PERIOD_CONCURRENT_UPDATE")
        _audit(session, "PaperRiskObserved", period, "risk")
        session.commit()
        return period


def suspend_period(account: str, currency: str) -> PaperPeriod:
    key = _key(account, currency)
    with _LOCK, session_factory()() as session:
        row = session.get(RiskPeriodRow, key)
        if row is None:
            raise RiskPeriodError("RISK_PERIOD_NOT_STARTED")
        period = PaperPeriod.model_validate(row.payload)
        period.suspended = True
        changed = session.execute(
            update(RiskPeriodRow)
            .where(
                RiskPeriodRow.account_key == key,
                RiskPeriodRow.version == row.version,
            )
            .values(version=row.version + 1, payload=period.model_dump(mode="json"))
        )
        if changed.rowcount != 1:
            raise RiskPeriodError("RISK_PERIOD_CONCURRENT_UPDATE")
        _audit(session, "PaperRiskPeriodSuspended", period, "user")
        session.commit()
        return period
