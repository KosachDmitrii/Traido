"""Exercise mark refresh and zone observation together, without broker access."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from core.schemas import Quote
from tests.unit.test_watch_poll_schedule import _watch
from trading import entry_watch_eval, entry_watch_loop, watch_marks


@pytest.mark.asyncio
async def test_zone_touch_uses_price_before_mark_refresh(monkeypatch):
    watch = _watch(price=103, lo=99, hi=101)
    rows = {watch.id: watch}

    class Store:
        def list_actionable(self):
            return list(rows.values())

        def get(self, key):
            return rows.get(key)

        def touch_mark(self, key, price):
            rows[key] = rows[key].model_copy(update={"last_price": price})
            return rows[key]

        def update(self, value):
            rows[value.id] = value

    class MD:
        async def get_quote(self, symbol):
            return Quote(symbol=symbol, bid=99.4, ask=99.6, ts=datetime.now(UTC), source="test")

    touches = []
    store = Store()
    monkeypatch.setattr(entry_watch_loop, "ENTRY_WATCHES", store)
    monkeypatch.setattr(watch_marks, "ENTRY_WATCHES", store)
    monkeypatch.setattr(entry_watch_loop, "create_market_data_port", lambda *_: MD())
    monkeypatch.setattr("trading.entry_watch_transitions.recover_stale_leases", lambda: None)
    monkeypatch.setattr(entry_watch_loop, "stale_invalidate_reason", lambda *_: None)
    monkeypatch.setattr(entry_watch_eval, "structure_lost_below_zone", lambda *_: False)
    monkeypatch.setattr(entry_watch_eval, "record_zone_touch", lambda key: touches.append(key))
    monkeypatch.setattr(entry_watch_eval, "zone_touch_exhausted", lambda *_: False)
    monkeypatch.setattr(entry_watch_eval, "zone_reclaim_met", lambda *_: False)

    async def cache(current, **kwargs):
        return current

    monkeypatch.setattr(entry_watch_loop, "refresh_watch_desk_cache", cache)
    monkeypatch.setattr(entry_watch_loop, "ensure_seeded_from_aftermath", lambda: None)
    monkeypatch.setattr(entry_watch_loop, "sync_from_paper_journal", lambda: None)
    monkeypatch.setattr(
        "trading.auto_trigger_policy.enqueue_auto_approve_open_buys", lambda **_: None
    )
    monkeypatch.setattr(entry_watch_loop, "create_audit", lambda: AsyncMock())
    stats = await entry_watch_loop.run_watch_pass()
    assert stats["still_waiting"] == 1
    assert touches == [watch.id]
    assert rows[watch.id].last_price == 99.5
    await entry_watch_loop.run_watch_pass()
    assert touches == [watch.id]
