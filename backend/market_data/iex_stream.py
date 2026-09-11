"""One IEX minute-bar stream; corrections upsert source bars, never place orders."""

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from core.clock import ET
from core.enums import Timeframe
from core.schemas import Bar
from market_data.bar_store import load, save, save_bars
from market_data.providers.alpaca import _bar_from_alpaca

logger = logging.getLogger(__name__)
_task: asyncio.Task[None] | None = None
_latest: dict[str, datetime] = {}
_connected = False
_connected_at: datetime | None = None
_completed: dict[str, datetime] = {}
_symbol_limit: int | None = None


def current(symbol: str, source: str, now: datetime) -> bool:
    ts = _latest.get(symbol)
    end = now.replace(minute=now.minute - now.minute % 5, second=0, microsecond=0)
    return bool(
        _connected
        and source == "alpaca:iex"
        and ts
        and 0 <= (now - ts).total_seconds() <= 90
        and _completed.get(symbol) == end - timedelta(minutes=5)
    )


def ingest(message: dict[str, Any], *, now: datetime) -> bool:
    """Require every source minute. No zero-volume or carried-price fabrication."""
    symbol = message.get("S")
    if not isinstance(symbol, str) or message.get("T") not in {"b", "u"}:
        return False
    try:
        if datetime.fromisoformat(message["t"]).tzinfo is None:
            return False
    except (KeyError, ValueError, TypeError):
        return False
    minute = _bar_from_alpaca(symbol, Timeframe.M5, message)
    if (
        minute is None
        or minute.ts.second
        or minute.ts.microsecond
        or minute.ts + timedelta(minutes=1) > now
    ):
        return False
    from strategy.orb import _valid_bar

    if not _valid_bar(minute):
        return False
    payload = minute.model_dump(mode="json")
    save("alpaca:iex", symbol, "1Min", [(minute.ts, payload)])
    start = minute.ts.replace(minute=minute.ts.minute - minute.ts.minute % 5)
    end = start + timedelta(minutes=5)
    rows = [Bar.model_validate(p) for p in load("alpaca:iex", symbol, "1Min", start, end)]
    if end > now or [b.ts for b in rows] != [start + timedelta(minutes=i) for i in range(5)]:
        return True
    bar = Bar(
        symbol=symbol,
        timeframe=Timeframe.M5,
        ts=start,
        open=rows[0].open,
        high=max(b.high for b in rows),
        low=min(b.low for b in rows),
        close=rows[-1].close,
        volume=sum((b.volume for b in rows), Decimal(0)),
        source="alpaca",
    )
    save_bars("alpaca:iex", symbol, [bar])
    logger.info("IEX completed bar stored: symbol=%s timestamp=%s", symbol, start.isoformat())
    if _connected_at is not None and start >= _connected_at:
        _completed[symbol] = max(start, _completed.get(symbol, start))
    return True


async def _run(key: str, secret: str) -> None:
    from websockets.asyncio.client import connect

    from strategy.orb.store import read_session

    global _connected, _connected_at, _symbol_limit
    wire_logger = logging.getLogger("market_data.iex_wire")
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
                "wss://stream.data.alpaca.markets/v2/iex",
                proxy=None,
                open_timeout=10,
                ping_interval=20,
                max_queue=1024,
                logger=wire_logger,
            ) as ws:
                await ws.send(json.dumps({"action": "auth", "key": key, "secret": secret}))
                authenticated = False
                subscribed = False
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except TimeoutError:
                        if not authenticated or not subscribed:
                            raise RuntimeError("IEX_STREAM_HANDSHAKE_TIMEOUT") from None
                        # A quiet feed must not imply fresh market data.
                        if day != str(datetime.now(ET).date()):
                            break
                        if (
                            list(
                                (await asyncio.to_thread(read_session, day) or {}).get("plans", {})
                            )[:_symbol_limit]
                            != symbols
                        ):
                            break
                        continue
                    messages = json.loads(raw)
                    for message in messages:
                        if message.get("T") == "error":
                            if message.get("code") == 405 and len(symbols) > 30:
                                # Respect Basic entitlements; the remaining plans
                                # continue independent REST recovery.
                                _symbol_limit = 30
                                symbols = symbols[:30]
                                await ws.send(
                                    json.dumps(
                                        {
                                            "action": "subscribe",
                                            "bars": symbols,
                                            "updatedBars": symbols,
                                        }
                                    )
                                )
                                logger.warning(
                                    "IEX stream symbol limit: using 30; remaining plans use REST"
                                )
                                continue
                            logger.warning("IEX stream rejected: code=%s", message.get("code"))
                            raise RuntimeError("IEX_STREAM_REJECTED")
                        if message.get("T") == "success" and message.get("msg") == "authenticated":
                            authenticated = True
                            await ws.send(
                                json.dumps(
                                    {"action": "subscribe", "bars": symbols, "updatedBars": symbols}
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
                                "IEX stream subscribed: symbols=%s confirmed=%s",
                                len(symbols),
                                subscribed,
                            )
                        elif (
                            subscribed
                            and message.get("S") in symbols
                            and message.get("T") in {"b", "u"}
                        ):
                            now = datetime.now(UTC)
                            if await asyncio.to_thread(ingest, message, now=now):
                                ts = datetime.fromisoformat(message["t"])
                                _latest[message["S"]] = max(
                                    ts + timedelta(minutes=1), _latest.get(message["S"], ts)
                                )
                    if day != str(datetime.now(ET).date()):
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — reconnect without leaking credentials
            # Exception bodies may contain handshake headers; log class only.
            logger.warning("IEX stream disconnected: %s; reconnect in 30s", type(exc).__name__)
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
    if settings.environment == "test" or resolve_alpaca_data_feed(settings) != "iex":
        return
    if settings.alpaca_api_key and settings.alpaca_api_secret and (_task is None or _task.done()):
        _task = asyncio.create_task(_run(settings.alpaca_api_key, settings.alpaca_api_secret))


async def stop() -> None:
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
