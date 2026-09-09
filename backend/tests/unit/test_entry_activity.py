"""Entry ownership must preserve the execution method calling convention."""

from uuid import uuid4

import pytest

from trading.entry_activity import entry_active, track_entry


@pytest.mark.asyncio
async def test_entry_ownership_accepts_keyword_arguments_and_releases_on_failure():
    class Service:
        @track_entry
        async def decide(self, opportunity_id, decision):
            assert entry_active(opportunity_id)
            if decision == "fail":
                raise ValueError("failed")
            return decision

    service = Service()
    key = uuid4()
    assert await service.decide(opportunity_id=key, decision="approve") == "approve"
    assert not entry_active(key)
    with pytest.raises(ValueError, match="failed"):
        await service.decide(opportunity_id=key, decision="fail")
    assert not entry_active(key)
