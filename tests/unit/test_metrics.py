"""The metrics registry, and the one way it could hurt the desk.

Metrics are additive and cannot reject a trade, so most of what matters here is
that they are correct and cheap. The exception is cardinality: a label whose
values are unbounded makes a new series per value, and a scanner that saw a
thousand symbols would then hold a thousand series that never expire. That is
the failure this file is mostly about.
"""

from __future__ import annotations

import pytest

from core.metrics import MAX_SERIES, MetricsRegistry


@pytest.fixture
def registry() -> MetricsRegistry:
    return MetricsRegistry()


def test_a_counter_accumulates(registry: MetricsRegistry) -> None:
    registry.counter("scans", 2)
    registry.counter("scans", 3)

    assert registry.snapshot()["counters"]["scans"]["_"] == 5


def test_a_gauge_replaces_rather_than_accumulates(registry: MetricsRegistry) -> None:
    """Universe size is a level, not a total. Adding it up says 14,000 names
    were scanned when the same 1,000 were scanned fourteen times."""
    registry.gauge("universe", 1000)
    registry.gauge("universe", 1010)

    assert registry.snapshot()["gauges"]["universe"]["_"] == 1010


def test_labels_separate_series(registry: MetricsRegistry) -> None:
    registry.counter("candidates", 5, labels={"stage": "stage1", "result": "passed"})
    registry.counter("candidates", 7, labels={"stage": "stage1", "result": "rejected"})

    series = registry.snapshot()["counters"]["candidates"]

    assert series["result=passed,stage=stage1"] == 5
    assert series["result=rejected,stage=stage1"] == 7


def test_label_order_does_not_create_a_second_series(registry: MetricsRegistry) -> None:
    registry.counter("c", 1, labels={"a": "1", "b": "2"})
    registry.counter("c", 1, labels={"b": "2", "a": "1"})

    assert len(registry.snapshot()["counters"]["c"]) == 1


def test_unbounded_labels_stop_growing_instead_of_leaking(registry: MetricsRegistry) -> None:
    """The backstop for a future caller labelling by symbol.

    Not a licence to do it — nothing in the scanner passes a symbol as a label.
    But a metrics endpoint that grows without limit is a memory leak that shows
    up as an outage weeks later, so the degradation is a dropped series.
    """
    for i in range(MAX_SERIES + 500):
        registry.counter("leaky", 1, labels={"symbol": f"SYM{i}"})

    assert len(registry.snapshot()["counters"]["leaky"]) == MAX_SERIES


def test_a_histogram_records_sum_count_and_buckets(registry: MetricsRegistry) -> None:
    registry.observe("scan_seconds", 3.0, buckets=(1, 5, 60))
    registry.observe("scan_seconds", 48.0, buckets=(1, 5, 60))

    hist = registry.snapshot()["histograms"]["scan_seconds"]["_"]

    assert hist["count"] == 2
    assert hist["sum"] == 51.0
    assert hist["buckets"] == {"1": 0, "5": 1, "60": 2}


def test_prometheus_output_is_parseable(registry: MetricsRegistry) -> None:
    registry.counter("traido_scans_total", 2, help_text="Cycles run.")
    registry.gauge("traido_universe_size", 1032)
    registry.observe("traido_scan_duration_seconds", 48.0, buckets=(30, 60))

    text = registry.render_prometheus()

    assert "# TYPE traido_scans_total counter" in text
    assert "traido_scans_total 2.0" in text
    assert "traido_universe_size 1032" in text
    assert 'traido_scan_duration_seconds_bucket{le="+Inf"} 1' in text
    assert "traido_scan_duration_seconds_count 1" in text


def test_histogram_buckets_are_cumulative(registry: MetricsRegistry) -> None:
    """Prometheus buckets are "less than or equal", not "in this band"."""
    registry.observe("d", 0.5, buckets=(1, 5, 60))
    registry.observe("d", 3.0, buckets=(1, 5, 60))

    text = registry.render_prometheus()

    assert 'd_bucket{le="1"} 1' in text
    assert 'd_bucket{le="5"} 2' in text
    assert 'd_bucket{le="60"} 2' in text


def test_a_label_value_cannot_break_the_exposition_format(registry: MetricsRegistry) -> None:
    """A quote or a newline in a label would produce a line no scraper can read."""
    registry.counter("c", 1, labels={"detail": 'we said "no"\nand meant it'})

    text = registry.render_prometheus()

    assert len(text.strip().splitlines()) == 2
    assert "\\" not in text


def test_the_registry_is_empty_after_a_reset(registry: MetricsRegistry) -> None:
    registry.counter("c", 1)
    registry.reset()

    assert registry.snapshot()["counters"] == {}


def test_the_scanner_records_a_cycle_without_touching_a_symbol_label() -> None:
    """The whole funnel goes to metrics; not one series is keyed by a ticker."""
    from agents.scanner.agent import _record_metrics
    from agents.scanner.cycle import CycleResult
    from core.metrics import METRICS

    METRICS.reset()
    result = CycleResult()
    result.funnel.universe_total = 1000
    result.funnel.structurally_eligible = 820
    result.funnel.market_filter_passed = 146
    result.funnel.quant_shortlisted = 30
    result.funnel.deep_analysis_started = 20
    result.funnel.risk_passed = 8
    result.funnel.published = 5
    result.published = ["AAPL", "MSFT"]

    _record_metrics(result)
    text = METRICS.render_prometheus()

    assert "traido_universe_size 1000" in text
    assert 'traido_scanner_candidates_total{result="passed",stage="stage1"} 146.0' in text
    for symbol in ("AAPL", "MSFT"):
        assert symbol not in text
    METRICS.reset()


def test_an_unbalanced_funnel_is_counted_loudly() -> None:
    from agents.scanner.agent import _record_metrics
    from agents.scanner.cycle import CycleResult
    from core.metrics import METRICS

    METRICS.reset()
    result = CycleResult()
    result.funnel.universe_total = 10  # nothing terminal recorded: a lost name

    _record_metrics(result)

    assert "traido_scan_funnel_unbalanced_total" in METRICS.render_prometheus()
    METRICS.reset()
