"""Who is allowed to run reconciliation, and how many of them at once.

Reconciliation is the only thing on this desk that reads broker truth and then
acts on it: it installs missing protective stops, resizes them, emergency-closes
what cannot be protected and cancels orphaned entries. That makes "how often does
it run" and "can two run at once" capital-safety questions rather than
performance ones.

Until now the answer to both came from an HTTP handler. `GET /api/v1/desk/broker`
drove it, `?fresh=true` skipped the interval, and nothing stopped two callers
overlapping — two dashboard tabs, a prefetch and a retried poll are all it takes.
`tests/integration/.../test_24` shows what that costs: two passes read the same
order book, both see the same missing stop, and both replace it.

This module owns the answer instead. One pass at a time, with concurrent callers
joining the pass already in flight rather than starting a second one, and the
result of every pass kept where the route and the gates can read it.

**Single process.** The guard is a process-wide threading primitive, so it holds
across every event loop and worker thread in this process — but not across two.
Two API replicas would still overlap, which is why the desk runs single-worker
(see `P1-6` and `P1-11` in the gap register): an invariant of the deployment,
not of this file.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from dataclasses import dataclass, replace
from typing import Any, Protocol

logger = logging.getLogger(__name__)

_JOIN_TIMEOUT_SEC = 30.0
"""How long a joining caller waits before answering with what it already has.

Bounded rather than indefinite: a pass wedged on an unresponsive broker socket
must not take every dashboard request down with it. The joining caller then
returns the previous status, which correctly reports itself as stale.
"""


class ReconcilePass(Protocol):
    """One reconciliation pass. Supplied by the caller so this module owns no vendors."""

    async def __call__(self) -> Any: ...


@dataclass(frozen=True)
class ReconciliationStatus:
    """What the last pass did, and how long ago.

    `last_success_wall` is deliberately wall-clock rather than monotonic: it
    crosses a process boundary into the desk payload and a human reads it. The
    scheduling decisions below use monotonic time, which does not jump when the
    host's clock is corrected.
    """

    ok: bool | None = None
    """`None` until a pass has completed. Not the same as `False`."""
    error: str | None = None
    last_success_wall: float | None = None
    last_success_mono: float | None = None
    last_attempt_wall: float | None = None
    running: bool = False
    passes: int = 0
    """Completed passes, successful or not. The count a duplicate-run test reads."""
    severity: str | None = None
    unresolved: tuple[str, ...] = ()

    @property
    def has_succeeded(self) -> bool:
        return self.last_success_mono is not None

    def age_seconds(self, *, now: float | None = None) -> float | None:
        """Seconds since the last *successful* pass, or `None` if there has been none.

        `None` is the honest answer for a process that has just started, and it
        is not interchangeable with a large number: a caller deciding whether
        broker truth is fresh enough must be able to tell "never checked" from
        "checked a long time ago", even though it refuses both.
        """
        if self.last_success_mono is None:
            return None
        return max(0.0, (now if now is not None else time.monotonic()) - self.last_success_mono)

    def as_dict(self) -> dict[str, Any]:
        age = self.age_seconds()
        return {
            "ok": self.ok,
            "error": self.error,
            "last_success_at": self.last_success_wall,
            "stale_seconds": None if age is None else round(age),
            "running": self.running,
            "passes": self.passes,
            "severity": self.severity,
            "unresolved": list(self.unresolved),
        }


class ReconciliationSupervisor:
    """Serialises reconciliation and remembers what the last pass found."""

    def __init__(self) -> None:
        self._status = ReconciliationStatus()
        # A threading primitive rather than an asyncio one, deliberately. The
        # guarantee wanted here is "one pass per process", and an `asyncio.Lock`
        # only delivers "one pass per event loop" — which is the same thing
        # under uvicorn today and stops being the same thing the moment anything
        # runs a pass from a worker thread or a second loop. Scoping the guard
        # to the process means it cannot be defeated by where the caller runs.
        self._gate = threading.Lock()
        self._inflight: threading.Event | None = None

    # ── Reading ──────────────────────────────────────────────────────────────

    @property
    def status(self) -> ReconciliationStatus:
        return self._status

    def age_seconds(self) -> float | None:
        return self._status.age_seconds()

    def reset(self) -> None:
        """Drop all history. For tests and for a deliberate re-arm, nothing else."""
        self._status = ReconciliationStatus()
        self._inflight = None

    # ── Running ──────────────────────────────────────────────────────────────

    async def run(self, pass_fn: ReconcilePass) -> ReconciliationStatus:
        """Run one pass, or join the one already running.

        Joining rather than queueing is the important part. A second caller that
        waited for the lock and then ran its own pass would still produce two
        passes, just not simultaneously — and two sequential passes against the
        same missing stop place two stops exactly as reliably as two concurrent
        ones. What the caller actually wants is *a recent pass*, and the one
        already in flight is that.
        """
        with self._gate:
            joining = self._inflight
            if joining is None:
                self._inflight = threading.Event()
                done = self._inflight

        if joining is not None:
            # Waited on a worker thread so this caller's event loop stays free —
            # under a single loop the pass it is waiting for is running on that
            # very loop, and blocking here would deadlock the thing it wants.
            await asyncio.to_thread(joining.wait, _JOIN_TIMEOUT_SEC)
            return self._status

        try:
            return await self._run_guarded(pass_fn)
        finally:
            with self._gate:
                self._inflight = None
            done.set()

    async def run_if_stale(
        self, pass_fn: ReconcilePass, *, max_age_sec: float
    ) -> ReconciliationStatus:
        """Run only when the last success is older than `max_age_sec`."""
        age = self.age_seconds()
        if age is not None and age < max_age_sec:
            return self._status
        return await self.run(pass_fn)

    async def _run_guarded(self, pass_fn: ReconcilePass) -> ReconciliationStatus:
        """Execute the pass and fold the outcome into the status. Never raises.

        A failure has to be recorded, not propagated: every caller of this is
        either an HTTP handler that must still answer or a background loop that
        must still be alive for the next tick. The failure is not swallowed — it
        becomes `ok=False` with the reason attached, which is what the desk
        renders and what the staleness gate refuses on.
        """
        self._status = replace(self._status, running=True, last_attempt_wall=time.time())
        try:
            report = await pass_fn()
        except Exception as exc:
            logger.warning("reconcile: pass failed", exc_info=True)
            self._status = replace(
                self._status,
                ok=False,
                error=f"{type(exc).__name__}: {exc}"[:200],
                running=False,
                passes=self._status.passes + 1,
            )
            asyncio.create_task(_emit_reconcile_alerts(self._status), name="reconcile-alerts")
            return self._status

        now_mono = time.monotonic()
        self._status = replace(
            self._status,
            ok=True,
            error=None,
            last_success_wall=time.time(),
            last_success_mono=now_mono,
            running=False,
            passes=self._status.passes + 1,
            severity=getattr(report, "severity", None),
            unresolved=tuple(getattr(report, "unresolved", ()) or ()),
        )
        # Fire-and-forget: waiting on Telegram must not stretch the single-flight
        # window or the background loop's tick.
        asyncio.create_task(_emit_reconcile_alerts(self._status), name="reconcile-alerts")
        return self._status


async def _emit_reconcile_alerts(status: ReconciliationStatus) -> None:
    """Surface capital-safety findings without depending on the dashboard."""
    from core.alerts import AlertSeverity, alert_if

    unresolved = status.unresolved
    if unresolved:
        sample = ", ".join(unresolved[:5])
        await alert_if(
            key=f"reconcile:unresolved:{status.severity or 'critical'}",
            severity=AlertSeverity.CRITICAL,
            title="Reconciliation unresolved",
            body=f"{len(unresolved)} item(s): {sample}",
            condition=True,
        )
    if not status.ok and status.error:
        await alert_if(
            key="reconcile:pass_failed",
            severity=AlertSeverity.CRITICAL,
            title="Reconciliation pass failed",
            body=status.error or "",
            condition=True,
        )


RECONCILE = ReconciliationSupervisor()
"""Process-wide supervisor. One desk, one reconciliation authority."""


# ── The control loop ─────────────────────────────────────────────────────────

RECONCILE_INTERVAL_SEC = 30.0
"""How often broker truth is re-read when nobody is watching the screen."""

MAX_RECONCILIATION_AGE_SEC = 180.0
"""How stale broker truth may be before new exposure is refused.

Six times the loop interval, which is deliberate slack rather than a round
number: a threshold near the interval would refuse entries during ordinary
jitter, and the operator's response to a gate that fires spuriously is to widen
it until it means nothing. Three minutes tolerates several missed ticks and
still catches a reconciliation that has genuinely stopped.

Overridable through `TRAIDO_MAX_RECONCILIATION_AGE_SEC` for operators who run a
different interval — not for switching the gate off, which is what
`require_fresh_reconciliation=False` on the service is for, and which is
recorded on every decision taken under it.
"""


def max_reconciliation_age() -> float:
    raw = os.getenv("TRAIDO_MAX_RECONCILIATION_AGE_SEC")
    if not raw:
        return MAX_RECONCILIATION_AGE_SEC
    try:
        value = float(raw)
    except ValueError:
        logger.warning("ignoring unparseable TRAIDO_MAX_RECONCILIATION_AGE_SEC=%r", raw)
        return MAX_RECONCILIATION_AGE_SEC
    return value if value > 0 else MAX_RECONCILIATION_AGE_SEC


_loop_task: asyncio.Task[None] | None = None


async def _reconcile_forever(pass_factory: Any, interval_sec: float) -> None:
    """Re-read broker truth on a timer, for as long as the process lives.

    Reconciliation used to run only inside the handler for `GET /desk/broker`,
    which made "is this desk checking itself against the broker" a question
    about whether a browser tab was open. Overnight the answer was no: protective
    orders unverified, orphaned positions undetected, `UNKNOWN` intents
    unresolved until someone logged in — and those are precisely the hours when
    a position sits unattended.

    The loop never dies of a bad pass. `run` records failures rather than
    raising them, and the one thing that can still escape — a factory that
    cannot build a broker at all — is caught here, because a supervisor that
    stops supervising after one bad tick is worse than none: the desk would look
    healthy and quietly stop checking.
    """
    logger.info("reconcile loop: started, every %.0fs", interval_sec)
    while True:
        try:
            await RECONCILE.run(pass_factory)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("reconcile loop: could not run a pass", exc_info=True)
        await asyncio.sleep(interval_sec)


def start_reconcile_loop(pass_factory: Any, *, interval_sec: float | None = None) -> None:
    """Begin the background loop. Idempotent — a second call is a no-op."""
    global _loop_task
    if _loop_task is not None and not _loop_task.done():
        return
    _loop_task = asyncio.create_task(
        _reconcile_forever(pass_factory, interval_sec or RECONCILE_INTERVAL_SEC)
    )


def stop_reconcile_loop() -> None:
    global _loop_task
    if _loop_task is not None:
        _loop_task.cancel()
        _loop_task = None


def reconcile_loop_running() -> bool:
    return _loop_task is not None and not _loop_task.done()
