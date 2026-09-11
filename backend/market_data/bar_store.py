"""Persist real bars with atomic correction upserts and exact provenance."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from core.enums import Timeframe
from core.schemas import Bar
from database.models.market_bars import MarketBarRow
from database.session import session_factory


def stamp(ts: datetime) -> str:
    if ts.tzinfo is None:
        raise ValueError("MARKET_BAR_TIMEZONE_MISSING")
    return ts.astimezone(UTC).isoformat()


def save(
    feed: str, symbol: str, timeframe: str, values: list[tuple[datetime, dict[str, Any]]]
) -> None:
    if not values:
        return
    with session_factory()() as db:
        if db.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            make_insert: Any = pg_insert
        else:
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            make_insert = sqlite_insert
        for ts, payload in values:
            stmt = make_insert(MarketBarRow).values(
                feed=feed, symbol=symbol, timeframe=timeframe, timestamp=stamp(ts), payload=payload
            )
            db.execute(
                stmt.on_conflict_do_update(
                    index_elements=["feed", "symbol", "timeframe", "timestamp"],
                    set_={"payload": stmt.excluded.payload},
                )
            )
        db.commit()


def load(
    feed: str, symbol: str, timeframe: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    with session_factory()() as db:
        return list(
            db.scalars(
                select(MarketBarRow.payload)
                .where(
                    MarketBarRow.feed == feed,
                    MarketBarRow.symbol == symbol,
                    MarketBarRow.timeframe == timeframe,
                    MarketBarRow.timestamp >= stamp(start),
                    MarketBarRow.timestamp < stamp(end),
                )
                .order_by(MarketBarRow.timestamp)
            ).all()
        )


def save_bars(feed: str, symbol: str, bars: list[Bar]) -> None:
    from strategy.orb import _valid_bar

    if any(
        b.symbol != symbol
        or b.source != "alpaca"
        or b.timeframe != Timeframe.M5
        or b.ts.tzinfo is None
        or b.ts.minute % 5
        or b.ts.second
        or b.ts.microsecond
        or not _valid_bar(b)
        for b in bars
    ):
        raise ValueError("ORB_RETEST_DATA_INVALID")
    save(feed, symbol, "5Min", [(b.ts, b.model_dump(mode="json")) for b in bars])


def load_bars(feed: str, symbol: str, start: datetime, end: datetime) -> list[Bar]:
    return [Bar.model_validate(p) for p in load(feed, symbol, "5Min", start, end)]
