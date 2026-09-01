"""Shutting the server down must end the event stream, not wait on it.

A graceful shutdown waits for in-flight responses. The desk's SSE stream is an
in-flight response designed never to finish, so the server waits for the stream
and the stream waits for the browser. Under `--reload` that means every code
change hangs until the last tab closes, and in production it means the process
does not come down when asked.

Two things are needed. The bus can ask streams to stop, which covers a shutdown
the app is told about. But uvicorn drains connections *before* it runs the
lifespan hook, so nothing inside the app speaks first — the stream also has to
expire on its own.
"""

from __future__ import annotations

import asyncio

import pytest

from api.routes import desk as desk_mod
from core.desk_bus import DeskBus


@pytest.fixture
def bus() -> DeskBus:
    return DeskBus()


async def test_closing_is_false_on_a_fresh_bus(bus: DeskBus) -> None:
    assert bus.closing is False


async def test_close_raises_the_flag(bus: DeskBus) -> None:
    bus.close()

    assert bus.closing is True


async def test_a_parked_stream_is_woken_immediately(bus: DeskBus) -> None:
    """The flag alone is not enough — a stream on `q.get()` cannot see it.

    Waiting for the keepalive timeout to notice would make shutdown slow rather
    than hung, which is better but still wrong.
    """
    q = bus.subscribe()

    bus.close()

    event = await asyncio.wait_for(q.get(), timeout=0.5)
    assert event["type"] == "closing"


async def test_every_open_stream_is_told(bus: DeskBus) -> None:
    """One tab left open must not be able to hold the server up."""
    queues = [bus.subscribe() for _ in range(3)]

    bus.close()

    for q in queues:
        event = await asyncio.wait_for(q.get(), timeout=0.5)
        assert event["type"] == "closing"


async def test_reopen_lets_a_later_app_stream_again(bus: DeskBus) -> None:
    """The bus is a module singleton and outlives any one app instance.

    Tests and reloads both start a second app in the same process; a bus stuck
    closed would serve them an immediately-terminating stream forever.
    """
    bus.close()
    bus.reopen()

    assert bus.closing is False


async def test_normal_events_still_flow(bus: DeskBus) -> None:
    """The shutdown path must not have replaced the bus's actual job."""
    q = bus.subscribe()

    bus.bump_desk(kind="scan_cycle", cycle=7)

    event = await asyncio.wait_for(q.get(), timeout=0.5)
    assert event["type"] == "scan_cycle"
    assert event["cycle"] == 7


class _ConnectedRequest:
    """A client that never hangs up — the case that used to wedge shutdown."""

    async def is_disconnected(self) -> bool:
        return False


async def _drain(response) -> list[str]:  # type: ignore[no-untyped-def]
    return [chunk async for chunk in response.body_iterator]


async def test_a_stream_ends_on_its_own_even_if_the_client_never_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bounded lifetime is what makes shutdown terminate at all.

    Uvicorn drains in-flight responses before the lifespan hook runs, so the
    bus signal never gets a turn. Only the stream expiring by itself puts a
    ceiling on how long a shutdown can take.
    """
    monkeypatch.setattr(desk_mod, "_STREAM_MAX_SEC", 0.05)

    response = await desk_mod.desk_stream(_ConnectedRequest())
    chunks = await asyncio.wait_for(_drain(response), timeout=2.0)

    assert any("hello" in c for c in chunks)


async def test_an_expiring_stream_asks_for_a_fast_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expiry is routine, so the desk must not go blind for the default retry."""
    monkeypatch.setattr(desk_mod, "_STREAM_MAX_SEC", 0.05)

    response = await desk_mod.desk_stream(_ConnectedRequest())
    chunks = await asyncio.wait_for(_drain(response), timeout=2.0)

    assert any(c.startswith("retry:") for c in chunks)


async def test_the_bus_still_unsubscribes_an_expired_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Otherwise a long-lived desk leaks a queue every two minutes."""
    monkeypatch.setattr(desk_mod, "_STREAM_MAX_SEC", 0.05)
    before = len(desk_mod.DESK_BUS._subs)

    response = await desk_mod.desk_stream(_ConnectedRequest())
    await asyncio.wait_for(_drain(response), timeout=2.0)

    assert len(desk_mod.DESK_BUS._subs) == before
