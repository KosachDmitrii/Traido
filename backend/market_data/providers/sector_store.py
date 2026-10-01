"""One latest classification observation per symbol, isolated from trade state."""

from copy import deepcopy
from typing import Any

from sqlalchemy import select

from database.models.sector import SectorClassificationRow
from database.session import session_factory


def read(symbol: str) -> dict[str, Any] | None:
    with session_factory()() as db:
        row = db.get(SectorClassificationRow, symbol)
        return deepcopy(row.payload) if row else None


def write(symbol: str, payload: dict[str, Any]) -> None:
    with session_factory()() as db:
        row = db.get(SectorClassificationRow, symbol)
        if row is None:
            db.add(SectorClassificationRow(symbol=symbol, payload=deepcopy(payload)))
        else:
            row.payload = deepcopy(payload)
        db.commit()


def observations() -> dict[str, dict[str, Any]]:
    with session_factory()() as db:
        return {
            row.symbol: deepcopy(row.payload) for row in db.scalars(select(SectorClassificationRow))
        }
