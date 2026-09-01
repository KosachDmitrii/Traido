"""Shared setup for unit tests.

Unit tests construct `ExecutionService` directly against fake brokers, so there
is no reconciliation loop running behind them and nothing for one to reconcile.
"""

from __future__ import annotations

import pytest

from trading.reconcile_supervisor import RECONCILE


@pytest.fixture(autouse=True)
def reconciliation_is_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer the freshness gate as a running desk would.

    New exposure is refused when the last successful reconciliation is older
    than the threshold, or has never happened — which is the state of every
    process that has not started its background loop, including a unit test.
    Without this the gate would be the first thing every entry test hits, and
    they would all be testing the cold-start refusal instead of what they are
    named for.

    Patched here rather than defaulted off in the constructor, deliberately. An
    optional argument that silently disables a capital gate when omitted is the
    exact shape of the liquidity-gate defect this programme exists to fix: the
    gate was written, tested and documented as enforced, and simply never handed
    the port it measures with. A test that wants the gate armed opts back in by
    restoring `RECONCILE.age_seconds`, and the behaviour itself is covered end
    to end in `tests/integration/test_entry_gates_end_to_end.py`.
    """
    monkeypatch.setattr(RECONCILE, "age_seconds", lambda: 0.0)
