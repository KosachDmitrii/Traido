"""Production-shaped budgets, durable rotation and broker-only exposure."""

import pytest

from agents.scanner.rotation import load_history, record_selection
from tests.scanner_fakes import (
    FakeUniverseProvider,
)
from universe.models import UniverseTier
from universe.service import UniverseService


@pytest.mark.asyncio
async def test_rotation_reaches_beyond_2000_with_cached_reference_and_restart():
    provider = FakeUniverseProvider(12500, otc_every=11, inactive_every=17)
    history = {}
    observed = set()
    first = None
    for turn in range(14):
        if turn in (0, 7):
            service = UniverseService(provider)
        snapshot = await service.get_scan_universe(
            tier=UniverseTier.BROAD,
            max_size=2000,
            last_seen=history.get("universe_selected"),
        )
        assert len(snapshot.eligible) == 2000
        assert all(i.active and not i.otc for i in snapshot.eligible)
        if first is None:
            first = set(snapshot.symbols)
        observed.update(snapshot.symbols)
        record_selection("universe_selected", snapshot.symbols)
        history = load_history()
    full = await service.get_universe(tier=UniverseTier.BROAD, max_size=0)
    assert observed == set(full.symbols)
    assert len(observed - first) > 8000
    assert provider.calls == 2
