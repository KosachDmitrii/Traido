"""When the next cycle starts.

The old loop was `scan; sleep(interval); repeat`. With a cycle that took four
minutes and an interval of five, that is not a five-minute cadence — it is a
nine-minute one, and nothing said so. Worse, the drift is silent and
proportional to how slow the cycle was, so the scanner ran least often exactly
when it had the most to do.

This is a cadence instead: cycle *n* is due at `start + n × interval`. Finishing
early means waiting for the slot; finishing late means the slot has passed, and
that is reported as an overrun rather than absorbed.

The overrun policy is one documented choice: **skip to the next future slot**,
never run back-to-back to catch up. A scanner behind schedule is a scanner
already struggling for provider capacity, and the last thing it should do is
immediately start again with no gap. Skipping keeps the phase stable, so cycles
stay aligned to the same points in the session however many were missed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class ScanSchedule:
    """A fixed-cadence clock for scan cycles.

    Monotonic throughout: a wall-clock adjustment mid-session must not make the
    next cycle due in the past or an hour away.
    """

    interval_sec: float
    _origin: float = field(default_factory=time.monotonic)
    _cycle: int = 0

    overruns: int = 0
    last_overrun_sec: float = 0.0
    last_scheduled_at: float | None = None
    last_started_at: float | None = None
    last_finished_at: float | None = None
    last_duration_sec: float = 0.0

    def next_due(self) -> float:
        return self._origin + self._cycle * self.interval_sec

    def seconds_until_due(self, now: float | None = None) -> float:
        return max(0.0, self.next_due() - (now if now is not None else time.monotonic()))

    def begin(self, now: float | None = None) -> float:
        """Claim the current slot. Returns how late this cycle is starting."""
        when = now if now is not None else time.monotonic()
        due = self.next_due()
        self.last_scheduled_at = due
        self.last_started_at = when
        return max(0.0, when - due)

    def complete(self, now: float | None = None) -> float:
        """Close the slot and advance to the next future one.

        Returns the overrun in seconds — how far past its own slot the cycle
        ran. Advancing to the next *future* slot rather than to `due + interval`
        is what stops a run of missed slots from queueing up as back-to-back
        cycles once the system recovers.
        """
        when = now if now is not None else time.monotonic()
        due = self.last_scheduled_at if self.last_scheduled_at is not None else self.next_due()
        self.last_finished_at = when
        if self.last_started_at is not None:
            self.last_duration_sec = when - self.last_started_at

        overrun = max(0.0, when - (due + self.interval_sec))
        if overrun > 0:
            self.overruns += 1
            self.last_overrun_sec = overrun
        else:
            self.last_overrun_sec = 0.0

        self._cycle += 1
        while self.next_due() <= when:
            self._cycle += 1
        return overrun

    def retarget(self, interval_sec: float, now: float | None = None) -> None:
        """Change cadence without letting the next slot land in the past."""
        if interval_sec <= 0 or interval_sec == self.interval_sec:
            return
        when = now if now is not None else time.monotonic()
        self.interval_sec = interval_sec
        self._origin = when
        self._cycle = 1

    def as_dict(self) -> dict[str, float | int | None]:
        now = time.monotonic()
        return {
            "interval_seconds": self.interval_sec,
            "seconds_until_next": round(self.seconds_until_due(now), 1),
            "last_duration_seconds": round(self.last_duration_sec, 2),
            "last_overrun_seconds": round(self.last_overrun_sec, 2),
            "overruns": self.overruns,
        }
