"""Counters, gauges and histograms, in process, with no new dependency.

There was no telemetry at all before this — `docs/production-readiness.md` says
so plainly — and a desk that scans a thousand names needs numbers that survive
the cycle that produced them. The activity board shows the last thing that
happened; this shows what has been happening.

Deliberately small. It is a registry and a Prometheus text exposition, not a
client library: adding `prometheus_client` would be a dependency and a decision
about a scrape architecture that nobody has taken yet, and the exposition format
is stable enough that taking it later costs nothing.

Cardinality is the one trap worth naming. A label whose values are unbounded —
a symbol, an order id, a timestamp — makes a new time series per value and turns
a metrics endpoint into a memory leak. So label values are bounded here by
construction: stage names, resource names, result classes. Nothing takes a
symbol, and `MAX_SERIES` is a backstop for the case where something does.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock

MAX_SERIES = 2000
"""Ceiling on distinct label combinations per metric.

Not a policy about how many are reasonable — it is far above anything the
current labels can produce. It exists so that a future caller passing a symbol
as a label degrades into a dropped series rather than into unbounded growth.
"""

_LABEL_SANITISE = str.maketrans({'"': "'", "\\": "/", "\n": " "})

type LabelKey = tuple[tuple[str, str], ...]
"""One series' labels, sorted, so label order cannot fork a series in two."""


def _key(labels: dict[str, str] | None) -> LabelKey:
    if not labels:
        return ()
    return tuple(sorted((k, str(v).translate(_LABEL_SANITISE)) for k, v in labels.items()))


@dataclass
class _Series:
    value: float = 0.0
    updated_at: float = field(default_factory=time.time)


@dataclass
class _Histogram:
    """Sum, count and bucket counts. Enough for a quantile, not a distribution.

    Buckets are seconds and fixed per metric: a cycle that takes 48 seconds and
    one that takes 400 must land in different buckets, and the interesting
    question about scan duration is which side of the cadence it fell on.
    """

    buckets: tuple[float, ...]
    counts: list[int]
    total: float = 0.0
    count: int = 0

    def observe(self, value: float) -> None:
        """Count the value in the narrowest bucket that holds it.

        Stored per-bucket rather than cumulatively; the exposition accumulates
        on the way out, because Prometheus buckets are "less than or equal".
        Doing it in both places counts every observation once per wider bucket.
        """
        self.total += value
        self.count += 1
        for i, edge in enumerate(self.buckets):
            if value <= edge:
                self.counts[i] += 1
                return


class MetricsRegistry:
    """One process's metrics. Thread-safe, because the API reads while the
    scanner writes."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[str, dict[LabelKey, _Series]] = {}
        self._gauges: dict[str, dict[LabelKey, _Series]] = {}
        self._histograms: dict[str, dict[LabelKey, _Histogram]] = {}
        self._help: dict[str, str] = {}

    def _room[T](self, family: dict[str, dict[LabelKey, T]], name: str, key: LabelKey) -> bool:
        series = family.setdefault(name, {})
        return key in series or len(series) < MAX_SERIES

    def counter(
        self,
        name: str,
        value: float = 1.0,
        *,
        labels: dict[str, str] | None = None,
        help_text: str = "",
    ) -> None:
        key = _key(labels)
        with self._lock:
            if help_text:
                self._help.setdefault(name, help_text)
            if not self._room(self._counters, name, key):
                return
            entry = self._counters[name].setdefault(key, _Series(0.0))
            entry.value += value
            entry.updated_at = time.time()

    def gauge(
        self,
        name: str,
        value: float,
        *,
        labels: dict[str, str] | None = None,
        help_text: str = "",
    ) -> None:
        key = _key(labels)
        with self._lock:
            if help_text:
                self._help.setdefault(name, help_text)
            if not self._room(self._gauges, name, key):
                return
            self._gauges[name][key] = _Series(value)

    def observe(
        self,
        name: str,
        value: float,
        *,
        buckets: tuple[float, ...] = (1, 5, 15, 30, 60, 120, 300, 600),
        labels: dict[str, str] | None = None,
        help_text: str = "",
    ) -> None:
        key = _key(labels)
        with self._lock:
            if help_text:
                self._help.setdefault(name, help_text)
            if not self._room(self._histograms, name, key):
                return
            hist = self._histograms[name].get(key)
            if hist is None:
                hist = _Histogram(buckets=buckets, counts=[0] * len(buckets))
                self._histograms[name][key] = hist
            hist.observe(value)

    # ── Reading ─────────────────────────────────────────────────────────────

    def snapshot(self) -> dict[str, dict[str, dict[str, object]]]:
        """Everything, as plain data. For the API and for tests."""
        with self._lock:
            return {
                "counters": {
                    name: {_render_key(k): s.value for k, s in series.items()}
                    for name, series in self._counters.items()
                },
                "gauges": {
                    name: {_render_key(k): s.value for k, s in series.items()}
                    for name, series in self._gauges.items()
                },
                "histograms": {
                    name: {
                        _render_key(k): {
                            "count": h.count,
                            "sum": round(h.total, 4),
                            # Cumulative, as the exposition renders them, so the
                            # JSON view and the scrape cannot disagree about
                            # what a bucket means.
                            "buckets": dict(
                                zip(
                                    (str(b) for b in h.buckets),
                                    _accumulate(h.counts),
                                    strict=True,
                                )
                            ),
                        }
                        for k, h in series.items()
                    }
                    for name, series in self._histograms.items()
                },
            }

    def render_prometheus(self) -> str:
        """Text exposition, so a scraper can be pointed at this later.

        Written by hand rather than via a client library: the format is a
        stable contract and this way the desk carries no scrape-time dependency
        it would have to keep current.
        """
        lines: list[str] = []
        with self._lock:
            for name, series in sorted(self._counters.items()):
                lines.extend(self._render_family(name, "counter", series))
            for name, series in sorted(self._gauges.items()):
                lines.extend(self._render_family(name, "gauge", series))
            for name, hists in sorted(self._histograms.items()):
                if help_text := self._help.get(name):
                    lines.append(f"# HELP {name} {help_text}")
                lines.append(f"# TYPE {name} histogram")
                for key, hist in hists.items():
                    cumulative = 0
                    for edge, count in zip(hist.buckets, hist.counts, strict=True):
                        cumulative += count
                        lines.append(
                            f"{name}_bucket{_labels_with(key, 'le', str(edge))} {cumulative}"
                        )
                    lines.append(f"{name}_bucket{_labels_with(key, 'le', '+Inf')} {hist.count}")
                    lines.append(f"{name}_sum{_render_labels(key)} {hist.total}")
                    lines.append(f"{name}_count{_render_labels(key)} {hist.count}")
        return "\n".join(lines) + "\n"

    def _render_family(self, name: str, kind: str, series: dict[LabelKey, _Series]) -> list[str]:
        lines = []
        if help_text := self._help.get(name):
            lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(f"{name}{_render_labels(key)} {entry.value}" for key, entry in series.items())
        return lines

    def reset(self) -> None:
        """For tests. Never called by the desk."""
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()
            self._help.clear()


def _accumulate(counts: list[int]) -> list[int]:
    running = 0
    out = []
    for count in counts:
        running += count
        out.append(running)
    return out


def _render_labels(key: LabelKey) -> str:
    if not key:
        return ""
    inner = ",".join(f'{k}="{v}"' for k, v in key)
    return "{" + inner + "}"


def _labels_with(key: LabelKey, name: str, value: str) -> str:
    return _render_labels((*key, (name, value)))


def _render_key(key: LabelKey) -> str:
    return ",".join(f"{k}={v}" for k, v in key) or "_"


METRICS = MetricsRegistry()
