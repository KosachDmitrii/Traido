"""One selected Alpaca minute-bar stream; corrections never place orders."""

import asyncio
import json
import logging
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from core.clock import ET
from core.enums import Timeframe
from core.schemas import Bar
from market_data.bar_store import load_many, save_bars_many, save_many
from market_data.providers.alpaca import _bar_from_alpaca

logger = logging.getLogger(__name__)
_task: asyncio.Task[None] | None = None
_latest: dict[str, datetime] = {}
_connected = False
_connected_at: datetime | None = None
_completed: dict[str, datetime] = {}
_symbol_limit: int | None = None
_feed = "sip"
STREAM_QUEUE_SIZE = 16384
STREAM_BATCH_SIZE = 500
STREAM_FLUSH_SECONDS = 0.05
STREAM_WINDOWS = 8192
STREAM_STATUS: dict[str, Any] = {}


def _source(feed: str | None = None) -> str:
    selected = (feed or _feed).strip().lower()
    if selected != "sip":
        raise ValueError("ALPACA_STREAM_FEED_INVALID")
    return f"alpaca:{selected}"


def current(symbol: str, source: str, now: datetime) -> bool:
    ts = _latest.get(symbol)
    end = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
    return bool(
        _connected
        and source == _source()
        and ts
        and 0 <= (now - ts).total_seconds() <= 90
        and _completed.get(symbol) == end - timedelta(minutes=5)
    )


def ingest(message: dict[str, Any], *, now: datetime, feed: str | None = None) -> bool:
    """Compatibility single-message ingestion; production keeps one bounded assembler."""
    return bool(StreamAssembler(feed).ingest_batch([message], now=now))


def _minute(message: dict[str, Any], now: datetime) -> Bar | None:
    symbol = message.get("S")
    if not isinstance(symbol, str) or message.get("T") not in {"b", "u"}:
        return None
    try:
        if datetime.fromisoformat(message["t"]).tzinfo is None:
            return None
    except (KeyError, ValueError, TypeError):
        return None
    minute = _bar_from_alpaca(symbol, Timeframe.M5, message)
    if (
        minute is None
        or minute.ts.second
        or minute.ts.microsecond
        or minute.ts + timedelta(minutes=1) > now
    ):
        return None
    from strategy.orb import _valid_bar

    if not _valid_bar(minute):
        return None
    return minute


class StreamAssembler:
    """FIFO correction semantics, real-minute aggregation and bounded restart recovery.

    Called by one writer only. Freshness and notifications advance only after
    both minute and completed-bar transactions succeed. On failure discard the
    connection's cache and recover through durable bars/REST on reconnect.
    """

    def __init__(self, feed: str | None = None) -> None:
        self.source = _source(feed)
        self.windows: OrderedDict[tuple[str, datetime], dict[datetime, Bar]] = OrderedDict()

    def ingest_batch(self, messages: list[dict[str, Any]], *, now: datetime) -> list[Bar]:
        minutes = [bar for message in messages if (bar := _minute(message, now)) is not None]
        missing: dict[datetime, set[str]] = {}
        for minute in minutes:
            start = minute.ts.replace(minute=minute.ts.minute - minute.ts.minute % 5)
            if (minute.symbol, start) not in self.windows:
                missing.setdefault(start, set()).add(minute.symbol)
        # One restart read per time window, not a query for every source minute.
        for start, symbols in missing.items():
            stored = load_many(
                self.source, sorted(symbols), "1Min", start, start + timedelta(minutes=5)
            )
            for symbol in symbols:
                self.windows[(symbol, start)] = {
                    bar.ts: bar
                    for payload in stored[symbol]
                    if (bar := Bar.model_validate(payload))
                }
        values: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
        touched = set()
        for minute in minutes:
            start = minute.ts.replace(minute=minute.ts.minute - minute.ts.minute % 5)
            key = (minute.symbol, start)
            self.windows[key][minute.ts] = minute
            self.windows.move_to_end(key)
            touched.add(key)
            values.setdefault(minute.symbol, []).append((minute.ts, minute.model_dump(mode="json")))
        save_many(self.source, "1Min", values)
        completed: dict[str, list[Bar]] = {}
        for symbol, start in sorted(touched):
            rows = self.windows[(symbol, start)]
            stamps = [start + timedelta(minutes=i) for i in range(5)]
            if start + timedelta(minutes=5) > now or sorted(rows) != stamps:
                continue
            ordered = [rows[ts] for ts in stamps]
            completed.setdefault(symbol, []).append(
                Bar(
                    symbol=symbol,
                    timeframe=Timeframe.M5,
                    ts=start,
                    open=ordered[0].open,
                    high=max(b.high for b in ordered),
                    low=min(b.low for b in ordered),
                    close=ordered[-1].close,
                    volume=sum((b.volume for b in ordered), Decimal(0)),
                    source="alpaca",
                )
            )
        save_bars_many(self.source, completed)
        from strategy.orb.runtime import notify_completed_bar

        for symbol, bars in completed.items():
            for bar in bars:
                notify_completed_bar(bar)
                if _connected_at is not None and bar.ts >= _connected_at:
                    _completed[symbol] = max(bar.ts, _completed.get(symbol, bar.ts))
        for minute in minutes:
            _latest[minute.symbol] = max(
                minute.ts + timedelta(minutes=1), _latest.get(minute.symbol, minute.ts)
            )
        while len(self.windows) > STREAM_WINDOWS:
            self.windows.popitem(last=False)
        if minutes:
            lag = max(0, (now - max(b.ts + timedelta(minutes=1) for b in minutes)).total_seconds())
            STREAM_STATUS.update(
                processed_at=now.isoformat(),
                source_lag_seconds=round(lag, 3),
                batch=len(minutes),
                completed=sum(map(len, completed.values())),
            )
            logger.info(
                "Alpaca SIP batch stored: minutes=%s completed=%s source_lag_seconds=%.3f",
                len(minutes),
                sum(map(len, completed.values())),
                lag,
            )
        return minutes


async def _write_stream(
    queue: asyncio.Queue[dict[str, Any] | None], stop: asyncio.Event, feed: str
) -> None:
    assembler = StreamAssembler(feed)
    while not stop.is_set() or not queue.empty():
        try:
            first = await asyncio.wait_for(queue.get(), timeout=STREAM_FLUSH_SECONDS)
        except TimeoutError:
            continue
        if first is None:
            return
        # Coalesce bursts without delaying an already complete vendor frame.
        if not stop.is_set():
            await asyncio.sleep(STREAM_FLUSH_SECONDS)
        messages = [first]
        while len(messages) < STREAM_BATCH_SIZE and not queue.empty():
            message = queue.get_nowait()
            if message is None:
                break
            messages.append(message)
        started = time.monotonic()
        write = asyncio.create_task(
            asyncio.to_thread(assembler.ingest_batch, messages, now=datetime.now(UTC))
        )
        try:
            await asyncio.shield(write)
        except asyncio.CancelledError:
            # to_thread cannot stop a transaction. Join it before any new writer
            # can process corrections or freshness for another connection.
            await write
            raise
        STREAM_STATUS.update(
            queue_depth=queue.qsize(), write_seconds=round(time.monotonic() - started, 3)
        )


async def _receive(ws: Any, writer: asyncio.Task[None]) -> str | bytes:
    """A failed writer must interrupt even a busy/quiet socket reader."""
    receive = asyncio.create_task(ws.recv())
    try:
        done, _ = await asyncio.wait(
            {receive, writer}, timeout=30, return_when=asyncio.FIRST_COMPLETED
        )
        if writer in done:
            await writer
            raise RuntimeError("ALPACA_STREAM_WRITER_STOPPED")
        if receive not in done:
            raise TimeoutError
        raw = await receive
        if not isinstance(raw, (str, bytes)):
            raise TypeError("ALPACA_STREAM_FRAME_INVALID")
        return raw
    finally:
        if not receive.done():
            receive.cancel()
        await asyncio.gather(receive, return_exceptions=True)


async def _run(key: str, secret: str, feed: str = "sip") -> None:
    from websockets.asyncio.client import connect

    from strategy.orb.store import read_runtime_session as read_session

    global _connected, _connected_at, _feed, _symbol_limit
    feed = feed.strip().lower()
    _source(feed)  # Validate before constructing the vendor URL.
    _feed = feed
    _symbol_limit = None
    wire_logger = logging.getLogger("market_data.alpaca_wire")
    wire_logger.setLevel(logging.WARNING)
    while True:
        try:
            day = str(datetime.now(ET).date())
            session = await asyncio.to_thread(read_session, day)
            symbols = list((session or {}).get("plans", {}))[:_symbol_limit]
            if not symbols:
                await asyncio.sleep(5)
                continue
            async with connect(
                f"wss://stream.data.alpaca.markets/v2/{feed}",
                proxy=None,
                open_timeout=10,
                ping_interval=20,
                max_queue=1024,
                logger=wire_logger,
            ) as ws:
                await ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
                queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(STREAM_QUEUE_SIZE)
                stop = asyncio.Event()
                writer = asyncio.create_task(
                    _write_stream(queue, stop, feed), name="alpaca-sip-writer"
                )
                try:
                    authenticated = False
                    subscribed = False
                    next_membership_check = time.monotonic() + 30
                    while True:
                        try:
                            raw = await _receive(ws, writer)
                        except TimeoutError:
                            if not authenticated or not subscribed:
                                raise RuntimeError("ALPACA_STREAM_HANDSHAKE_TIMEOUT") from None
                            # A quiet feed must not imply fresh market data.
                            if day != str(datetime.now(ET).date()):
                                break
                            if (
                                list(
                                    (await asyncio.to_thread(read_session, day) or {}).get(
                                        "plans", {}
                                    )
                                )[:_symbol_limit]
                                != symbols
                            ):
                                break
                            continue
                        messages = json.loads(raw)
                        for message in messages:
                            if message.get("T") == "error":
                                logger.warning(
                                    "Alpaca %s stream rejected: code=%s", feed, message.get("code")
                                )
                                raise RuntimeError("ALPACA_STREAM_REJECTED")
                            if (
                                message.get("T") == "success"
                                and message.get("msg") == "authenticated"
                            ):
                                authenticated = True
                                await ws.send(
                                    json.dumps(
                                        {
                                            "action": "subscribe",
                                            "bars": symbols,
                                            "updatedBars": symbols,
                                        }
                                    )
                                )
                            elif message.get("T") == "subscription":
                                subscribed = (
                                    authenticated
                                    and set(symbols).issubset(message.get("bars", []))
                                    and set(symbols).issubset(message.get("updatedBars", []))
                                )
                                _connected = subscribed
                                _connected_at = datetime.now(UTC) if subscribed else None
                                logger.info(
                                    "Alpaca %s stream subscribed: symbols=%s confirmed=%s",
                                    feed,
                                    len(symbols),
                                    subscribed,
                                )
                            elif (
                                subscribed
                                and message.get("S") in symbols
                                and message.get("T") in {"b", "u"}
                            ):
                                try:
                                    queue.put_nowait(message)
                                except asyncio.QueueFull:
                                    # Invalidate stream freshness and reconnect. REST
                                    # recovery must prove any uncommitted interval.
                                    _connected = False
                                    STREAM_STATUS["error"] = "ALPACA_STREAM_QUEUE_FULL"
                                    logger.warning(
                                        "Alpaca SIP queue overflow: pending=%s; reconnect for REST recovery",
                                        queue.qsize(),
                                    )
                                    raise RuntimeError("ALPACA_STREAM_QUEUE_FULL") from None
                        if day != str(datetime.now(ET).date()):
                            break
                        # An active feed never reaches recv's idle timeout. Check
                        # membership on elapsed time too, so newly discovered
                        # companies receive bars during a busy market session.
                        if subscribed and time.monotonic() >= next_membership_check:
                            wanted = list(
                                (await asyncio.to_thread(read_session, day) or {}).get("plans", {})
                            )[:_symbol_limit]
                            added = sorted(set(wanted) - set(symbols))
                            if added:
                                await ws.send(
                                    json.dumps(
                                        {"action": "subscribe", "bars": added, "updatedBars": added}
                                    )
                                )
                                symbols = list(dict.fromkeys([*symbols, *added]))
                                logger.info(
                                    "Alpaca %s stream expanded: added=%s total=%s",
                                    feed,
                                    len(added),
                                    len(symbols),
                                )
                            next_membership_check = time.monotonic() + 30
                finally:
                    _connected = False
                    stop.set()
                    if not queue.full():
                        queue.put_nowait(None)
                    try:
                        await asyncio.wait_for(asyncio.shield(writer), timeout=5)
                    except BaseException:
                        writer.cancel()
                        await asyncio.gather(writer, return_exceptions=True)
                        raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — reconnect without leaking credentials
            # Exception bodies may contain handshake headers; log class only.
            logger.warning(
                "Alpaca %s stream disconnected: %s; reconnect in 30s",
                feed,
                type(exc).__name__,
            )
        finally:
            _connected = False
            _connected_at = None
            _latest.clear()
            _completed.clear()
        await asyncio.sleep(30)


def start() -> None:
    from core.config import get_settings
    from market_data.factory import resolve_alpaca_data_feed

    global _task
    settings = get_settings()
    feed = resolve_alpaca_data_feed(settings)
    if settings.environment == "test" or feed != "sip":
        return
    if settings.alpaca_api_key and settings.alpaca_api_secret and (_task is None or _task.done()):
        _task = asyncio.create_task(_run(settings.alpaca_api_key, settings.alpaca_api_secret, feed))


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
