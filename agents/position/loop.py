"""Who watches open positions, and when.

The position agent used to run inside the handler for `GET /api/v1/desk/broker`,
which made "is anything looking at these positions" a question about whether a
browser tab was open. Reconciliation was moved off that handler for exactly this
reason and the exit assessment was left behind, so the two halves of watching a
position — is it still protected, and should it still be held — ran on different
schedules: one on a timer, one on a page render.

What that costs is quieter than a duplicated stop but not smaller. A proposal
that should have been raised at 15:40 is raised whenever someone next looks, and
a proposal that should have been *withdrawn* stays on the board until then,
which is worse: the operator returns to a sell card whose reason stopped holding
hours ago and has no way to tell. Withdrawal is a control action, and control
actions cannot be driven by a page render.

Deliberately a separate loop rather than another step inside the reconciliation
pass. Reconciliation acts on broker truth and must run even when market data is
unreadable; the exit assessment reads market data and refuses when it is stale.
Folding one into the other would make a vendor outage on the quote feed able to
delay a protective stop, which is the wrong dependency to introduce for the
convenience of one timer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

POSITION_INTERVAL_SEC = 60.0
"""How often open positions are re-judged when nobody is watching the screen.

Slower than reconciliation's thirty seconds, because the two answer questions of
different urgency. Reconciliation asks whether a position is protected at all,
and an unprotected position is an open-ended loss. This asks whether a protected
position should be closed early, which is a question about giving back part of a
bounded outcome — and the stop is resting at the broker the whole time.
"""

_loop_task: asyncio.Task[None] | None = None


async def _assess_forever(pass_factory: Any, interval_sec: float) -> None:
    """Re-judge open positions on a timer, for as long as the process lives.

    Never dies of a bad pass. A vendor outage, an unreadable ledger row or a
    broker that will not answer must leave the loop alive for the next tick: a
    supervisor that stops supervising after one bad tick is worse than none,
    because the desk goes on looking healthy.
    """
    logger.info("position loop: started, every %.0fs", interval_sec)
    while True:
        try:
            await pass_factory()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("position loop: could not assess exits", exc_info=True)
        await asyncio.sleep(interval_sec)


def start_position_loop(pass_factory: Any, *, interval_sec: float | None = None) -> None:
    """Begin the background loop. Idempotent — a second call is a no-op."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    _loop_task = asyncio.create_task(
        _assess_forever(pass_factory, interval_sec or POSITION_INTERVAL_SEC)
    )


def stop_position_loop() -> None:
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None


def position_loop_running() -> bool:
    return _loop_task is not None and not _loop_task.done()
