"""Position ledger + live journal helpers (Stage 5)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from threading import Lock
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from core.enums import TradingMode
from core.schemas import TradeOpportunity
from database.models.journal import TradeJournalRow
from database.models.positions import OpenPositionRow
from database.session import session_factory


def _session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    """Prepared once per engine — see `database.session.session_factory`."""
    return session_factory(engine)


def _blended_exit_price(legs: list[dict[str, Any]], *, fallback: Decimal) -> Decimal:
    """Quantity-weighted exit across every leg of a staged exit.

    Journalling only the last leg's price would report a PnL that no trade
    produced whenever a position left in more than one piece.
    """
    total = sum((Decimal(str(leg["qty"])) for leg in legs), Decimal(0))
    if total <= 0:
        return fallback
    notional = sum(
        (Decimal(str(leg["qty"])) * Decimal(str(leg["price"])) for leg in legs), Decimal(0)
    )
    return notional / total


class DuplicateOpenPosition(RuntimeError):
    """A second open row for a symbol the book already holds.

    Brokers report one net position per symbol, so two local rows can never
    agree with broker truth: reconciliation compares each row against the same
    single broker position and finds both of them wrong. `find_open_by_symbol`
    would also silently answer with whichever row happens to sort first, which
    is how a stop gets sized against half a position.
    """


@dataclass(frozen=True)
class ExitApplication:
    """Outcome of applying one exit fill to the book.

    `closed` is the fact callers actually branch on: a partial exit leaves a
    real position behind that still needs protection, and treating it as a
    close is how an unprotected remainder happens.
    """

    position_id: uuid.UUID | None
    closed: bool
    filled_qty: Decimal
    remaining_qty: Decimal
    journal: TradeJournalRow | None = None
    found: bool = True


def _realised_risk_reward(*, entry: Decimal, stop: Decimal, target: Decimal) -> float | None:
    """Reward over risk at the price actually paid. `None` if there is no risk."""
    risk = Decimal(str(entry)) - Decimal(str(stop))
    if risk <= 0:
        return None
    return round(float((Decimal(str(target)) - Decimal(str(entry))) / risk), 2)


class PositionLedger:
    def __init__(self, engine: Engine | None = None) -> None:
        self._engine = engine
        self._lock = Lock()

    def open_from_opportunity(
        self,
        opp: TradeOpportunity,
        *,
        qty: Decimal,
        broker_entry_order_id: str | None,
        fill_price: Decimal | None = None,
        stop_order_id: str | None = None,
    ) -> OpenPositionRow:
        SessionLocal = _session_factory(self._engine)
        entry = fill_price if fill_price is not None else opp.candidate.entry
        row = OpenPositionRow(
            id=uuid.uuid4(),
            opportunity_id=opp.id,
            symbol=opp.candidate.symbol.upper(),
            qty=qty,
            avg_entry=entry,
            stop_price=opp.candidate.stop,
            target_price=opp.candidate.target,
            strategy_version=opp.candidate.strategy_version,
            trading_mode=opp.trading_mode.value,
            status="open",
            entry_reasons=list(opp.candidate.reasons),
            broker_entry_order_id=broker_entry_order_id,
            payload={
                "confidence": opp.candidate.confidence,
                # Measured from the price actually paid, not copied from the
                # card. The two differ whenever the entry was repriced or the
                # fill improved on the limit, and the card's number is the one
                # that was never true: four positions opened on 2026-08-31 all
                # recorded 2.0, having been taken at 2.04, 1.97, 1.53 and 0.32.
                # The journal is what the strategy is later judged by, so it
                # records the trade that happened.
                "risk_reward": _realised_risk_reward(
                    entry=entry, stop=opp.candidate.stop, target=opp.candidate.target
                ),
                "card_risk_reward": opp.candidate.risk_reward,
                "pipeline_run_id": str(opp.candidate.pipeline_run_id)
                if opp.candidate.pipeline_run_id
                else None,
                "fill_price": str(entry),
                "stop_order_id": stop_order_id,
                "planned_entry": str(opp.candidate.entry),
                # Kept so the position agent can judge the exit on the series
                # the entry was drawn on, rather than defaulting to daily and
                # comparing two different SMA20s.
                "exec_timeframe": (
                    opp.candidate.exec_timeframe.value
                    if opp.candidate.exec_timeframe is not None
                    else None
                ),
            },
            opened_at=datetime.now(UTC),
        )
        with self._lock, SessionLocal() as session:
            # Checked inside the same lock and session as the insert, so two
            # concurrent entries cannot both find the book empty.
            clash = self._open_clash(session, row.symbol)
            if clash is not None:
                raise DuplicateOpenPosition(
                    f"{row.symbol} already has an open position ({clash.id}, qty {clash.qty})"
                )
            session.add(row)
            try:
                session.commit()
            except IntegrityError as exc:
                # The lock above only covers this process. A second API worker
                # or a scanner in its own container passes the read cleanly and
                # is stopped here, by the partial unique index, so the caller
                # sees the same refusal either way rather than a stray database
                # error at the top of the entry path.
                session.rollback()
                raise DuplicateOpenPosition(
                    f"{row.symbol} already has an open position "
                    "(refused by the database, another process opened it first)"
                ) from exc
            session.refresh(row)
            return row

    def _open_clash(self, session: Session, symbol: str) -> OpenPositionRow | None:
        """The open row already held for this symbol, if any."""
        return (
            session.query(OpenPositionRow)
            .filter(OpenPositionRow.symbol == symbol, OpenPositionRow.status == "open")
            .first()
        )

    def get_open(self, symbol: str | None = None) -> list[OpenPositionRow]:
        SessionLocal = _session_factory(self._engine)
        with SessionLocal() as session:
            q = session.query(OpenPositionRow).filter(OpenPositionRow.status == "open")
            if symbol:
                q = q.filter(OpenPositionRow.symbol == symbol.upper())
            return list(q.order_by(OpenPositionRow.opened_at.desc()).all())

    def get(self, position_id: uuid.UUID) -> OpenPositionRow | None:
        SessionLocal = _session_factory(self._engine)
        with SessionLocal() as session:
            return session.get(OpenPositionRow, position_id)

    def find_open_by_symbol(self, symbol: str) -> OpenPositionRow | None:
        rows = self.get_open(symbol)
        return rows[0] if rows else None

    def set_stop_order_id(self, position_id: uuid.UUID, stop_order_id: str) -> None:
        """Point a position at its current protective stop after recovery."""
        SessionLocal = _session_factory(self._engine)
        with SessionLocal() as session:
            row = session.get(OpenPositionRow, position_id)
            if row is None:
                return
            payload = dict(row.payload or {})
            payload["stop_order_id"] = stop_order_id
            row.payload = payload
            session.commit()

    def apply_exit_fill(
        self,
        *,
        symbol: str,
        filled_qty: Decimal,
        exit_price: Decimal,
        exit_reasons: list[str],
        position_id: uuid.UUID | None = None,
    ) -> ExitApplication:
        """Reduce the position by exactly what the broker filled.

        The local book has one job here: agree with the broker. A sell of 30 out
        of 100 leaves 70 shares that are still ours, still exposed, and still
        owed a protective stop — so the position stays open and only the
        quantity moves. Only a fill that takes the position to zero closes it
        and writes the journal, with the exit price blended across every leg so
        the recorded PnL is the one that actually happened.
        """
        SessionLocal = _session_factory(self._engine)
        with self._lock, SessionLocal() as session:
            row = self._open_row(session, symbol=symbol, position_id=position_id)
            if row is None:
                return ExitApplication(
                    position_id=position_id,
                    closed=False,
                    filled_qty=Decimal(0),
                    remaining_qty=Decimal(0),
                    found=False,
                )

            held = Decimal(str(row.qty))
            # Never let a broker over-report shrink the book past empty.
            applied = min(Decimal(str(filled_qty)), held)
            remaining = held - applied
            payload = dict(row.payload or {})
            legs = list(payload.get("exit_legs") or [])
            legs.append({"qty": str(applied), "price": str(exit_price)})
            payload["exit_legs"] = legs

            if remaining > 0:
                row.qty = remaining
                row.payload = payload
                session.commit()
                return ExitApplication(
                    position_id=row.id,
                    closed=False,
                    filled_qty=applied,
                    remaining_qty=remaining,
                )

            journal = self._journal(
                session,
                row,
                exit_price=_blended_exit_price(legs, fallback=exit_price),
                close_qty=sum((Decimal(str(leg["qty"])) for leg in legs), Decimal(0)),
                exit_reasons=exit_reasons,
                payload=payload,
            )
            session.commit()
            session.refresh(journal)
            return ExitApplication(
                position_id=row.id,
                closed=True,
                filled_qty=applied,
                remaining_qty=Decimal(0),
                journal=journal,
            )

    @staticmethod
    def _open_row(
        session: Session,
        *,
        symbol: str,
        position_id: uuid.UUID | None,
    ) -> OpenPositionRow | None:
        """Resolve the position to act on, refusing to touch a closed one.

        Filtering on `status == "open"` even when an id is supplied is what makes
        double-closing impossible: the second attempt simply finds nothing.
        """
        if position_id is not None:
            row = session.get(OpenPositionRow, position_id)
            return row if row is not None and row.status == "open" else None
        return (
            session.query(OpenPositionRow)
            .filter(
                OpenPositionRow.symbol == symbol.upper(),
                OpenPositionRow.status == "open",
            )
            .order_by(OpenPositionRow.opened_at.desc())
            .first()
        )

    def set_quantity(self, position_id: uuid.UUID, qty: Decimal) -> None:
        """Force local quantity to match broker truth after a reconciled discrepancy."""
        SessionLocal = _session_factory(self._engine)
        with SessionLocal() as session:
            row = session.get(OpenPositionRow, position_id)
            if row is None or row.status != "open":
                return
            row.qty = qty
            session.commit()

    def close_and_journal(
        self,
        *,
        symbol: str,
        exit_price: Decimal,
        exit_reasons: list[str],
        qty: Decimal | None = None,
    ) -> TradeJournalRow | None:
        """Mark Traido ledger position closed and append trade_journal row."""
        SessionLocal = _session_factory(self._engine)
        with self._lock, SessionLocal() as session:
            row = (
                session.query(OpenPositionRow)
                .filter(
                    OpenPositionRow.symbol == symbol.upper(),
                    OpenPositionRow.status == "open",
                )
                .order_by(OpenPositionRow.opened_at.desc())
                .first()
            )
            if row is None:
                return None

            journal = self._journal(
                session,
                row,
                exit_price=exit_price,
                close_qty=Decimal(str(qty if qty is not None else row.qty)),
                exit_reasons=exit_reasons,
                payload=dict(row.payload or {}),
            )
            session.commit()
            session.refresh(journal)
            return journal

    @staticmethod
    def _journal(
        session: Session,
        row: OpenPositionRow,
        *,
        exit_price: Decimal,
        close_qty: Decimal,
        exit_reasons: list[str],
        payload: dict[str, Any],
    ) -> TradeJournalRow:
        entry = Decimal(str(row.avg_entry))
        pnl = (exit_price - entry) * close_qty
        pnl_pct = float((exit_price - entry) / entry * 100) if entry else 0.0
        now = datetime.now(UTC)

        journal = TradeJournalRow(
            id=uuid.uuid4(),
            backtest_run_id=None,
            position_id=row.id,
            symbol=row.symbol,
            entry=entry,
            exit=exit_price,
            stop=row.stop_price,
            target=row.target_price,
            qty=close_qty,
            pnl=pnl,
            pnl_pct=pnl_pct,
            entry_reasons=list(row.entry_reasons or []),
            exit_reasons=exit_reasons,
            strategy_version=row.strategy_version,
            trading_mode=row.trading_mode or TradingMode.CONFIRMATION.value,
            indicators_at_entry={},
            assessments_at_entry=payload,
            risk_reward_planned=payload.get("risk_reward"),
            opened_at=row.opened_at,
            closed_at=now,
        )
        row.status = "closed"
        row.closed_at = now
        row.qty = Decimal(0)
        row.payload = payload
        session.add(journal)
        return journal

    def list_closed_journal(self, *, limit: int = 100) -> list[TradeJournalRow]:
        SessionLocal = _session_factory(self._engine)
        with SessionLocal() as session:
            return list(
                session.query(TradeJournalRow)
                .filter(TradeJournalRow.backtest_run_id.is_(None))
                .order_by(TradeJournalRow.closed_at.desc().nullslast())
                .limit(limit)
                .all()
            )


LEDGER = PositionLedger()
