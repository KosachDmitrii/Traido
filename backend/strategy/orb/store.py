"""Session selection is write-once; updates cannot move entry/stop geometry."""

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from database.models.orb import OrbSessionRow
from database.session import session_factory


def read_session(day: str) -> dict[str, Any] | None:
    with session_factory()() as db:
        row = db.get(OrbSessionRow, day)
        return deepcopy(row.payload) if row else None


def create_session(day: str, payload: dict[str, Any]) -> dict[str, Any]:
    with session_factory()() as db:
        row = db.get(OrbSessionRow, day)
        if row:
            return deepcopy(row.payload)
        db.add(OrbSessionRow(session=day, payload=deepcopy(payload)))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            winner = db.get(OrbSessionRow, day)
            if winner is None:
                raise
            return deepcopy(winner.payload)
        return deepcopy(payload)


def update_state(day: str, symbol: str, state: dict[str, Any]) -> None:
    with session_factory()() as db:
        row = db.scalar(select(OrbSessionRow).where(OrbSessionRow.session == day).with_for_update())
        if row is None or symbol not in row.payload.get("plans", {}):
            raise ValueError("ORB_PLAN_NOT_FOUND")
        payload = deepcopy(row.payload)
        current = payload.get("states", {}).get(symbol, {})
        payload.setdefault("states", {})[symbol] = {**current, **state}
        if current.get("opportunity_id"):
            payload["states"][symbol]["opportunity_id"] = current["opportunity_id"]
        row.payload = payload
        db.commit()
