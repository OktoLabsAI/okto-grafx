"""The JSON metrics adapter (SPEC-M1 FR-14, OR-6: a selectable adapter with no code shortcut).

The document is the same measurement the text body carries, in a shape a batch job or a CI runner
can read without an HTTP scraper, so the round trip through ``json.loads`` is the real assertion
here. The file writer is tested against a real temporary directory because rotation is mechanism
and mechanism is only proved by running it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from okto_grafx.adapters.metrics_json import (
    DOCUMENT_FORMAT,
    DOCUMENT_VERSION,
    JsonMetricsSink,
    RotatingFileWriter,
)
from okto_grafx.adapters.metrics_openmetrics import MetricAggregator, OpenMetricsSink
from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ports.metrics import MetricDescriptor, MetricKind, MetricsSink
from okto_grafx.engine.metrics_catalog import metric_names, register_catalog

PROBE_HISTOGRAM = MetricDescriptor(
    name="oktografx_probe_seconds",
    kind=MetricKind.HISTOGRAM,
    description="A probe histogram.",
    unit="seconds",
    buckets=(1.0, 2.0),
)


class _Collector:
    """A destination that keeps every document it was handed."""

    def __init__(self) -> None:
        self.documents: list[str] = []

    def __call__(self, document: str) -> None:
        self.documents.append(document)


def test_the_sink_satisfies_the_port() -> None:
    assert isinstance(JsonMetricsSink(_Collector()), MetricsSink)


def test_a_destination_that_is_not_callable_is_refused() -> None:
    with pytest.raises(GrafxConfigurationError, match="callable destination"):
        JsonMetricsSink("metrics.json")  # type: ignore[arg-type]


def test_publish_hands_one_json_document_to_the_destination() -> None:
    collector = _Collector()
    sink = JsonMetricsSink(collector)
    register_catalog(sink)
    sink.increment("oktografx_database_opens_total", 2.0)
    sink.set_gauge("oktografx_ledger_depth", 3.0, {"origin_class": "forensic"})

    returned = sink.publish()
    assert collector.documents == [returned]

    document = json.loads(returned)
    assert document["format"] == DOCUMENT_FORMAT
    assert document["version"] == DOCUMENT_VERSION
    assert {entry["name"] for entry in document["metrics"]} == metric_names()

    by_name = {entry["name"]: entry for entry in document["metrics"]}
    opens = by_name["oktografx_database_opens_total"]
    assert opens["kind"] == "counter"
    assert opens["samples"] == [{"labels": {}, "value": 2.0}]
    ledger = by_name["oktografx_ledger_depth"]
    assert ledger["unit"] == "entries"
    assert ledger["samples"] == [{"labels": {"origin_class": "forensic"}, "value": 3.0}]


def test_a_histogram_round_trips_with_its_cumulative_buckets() -> None:
    collector = _Collector()
    sink = JsonMetricsSink(collector)
    sink.register(PROBE_HISTOGRAM)
    for observation in (0.5, 1.5, 4.5):
        sink.observe("oktografx_probe_seconds", observation)

    entry = json.loads(sink.publish())["metrics"][0]
    assert entry["kind"] == "histogram"
    assert entry["samples"][0]["count"] == 3.0
    assert entry["samples"][0]["sum"] == 6.5
    assert entry["samples"][0]["buckets"] == [
        {"le": "1", "count": 1.0},
        {"le": "2", "count": 2.0},
        {"le": "+Inf", "count": 3.0},
    ]


def test_the_document_is_stable_and_sorted() -> None:
    collector = _Collector()
    sink = JsonMetricsSink(collector)
    register_catalog(sink)
    sink.set_gauge("oktografx_vector_index_entries", 1.0, {"space": "beta"})
    sink.set_gauge("oktografx_vector_index_entries", 2.0, {"space": "alpha"})
    document = json.loads(sink.document())
    names = [entry["name"] for entry in document["metrics"]]
    assert names == sorted(names)
    entries = {entry["name"]: entry for entry in document["metrics"]}
    spaces = [sample["labels"]["space"] for sample in entries["oktografx_vector_index_entries"]["samples"]]
    assert spaces == ["alpha", "beta"]
    assert sink.document() == sink.document()


def test_the_registration_contract_is_the_same_as_the_text_adapter() -> None:
    sink = JsonMetricsSink(_Collector())
    register_catalog(sink)
    with pytest.raises(GrafxConfigurationError, match="was never registered"):
        sink.increment("oktografx_ghost_total")
    with pytest.raises(GrafxConfigurationError, match="is not one of them"):
        sink.set_gauge("oktografx_ledger_depth", 1.0, {"origin_class": "unknown"})
    with pytest.raises(GrafxConfigurationError, match="cannot be used as a gauge"):
        sink.set_gauge("oktografx_database_opens_total", 1.0)


def test_a_disabled_sink_publishes_declarations_with_no_samples() -> None:
    sink = JsonMetricsSink(_Collector(), enabled=False)
    register_catalog(sink)
    assert sink.enabled is False
    sink.increment("oktografx_database_opens_total")
    document = json.loads(sink.document())
    assert all(entry["samples"] == [] for entry in document["metrics"])


def test_the_sink_can_share_the_aggregator_of_the_text_adapter() -> None:
    aggregator = MetricAggregator()
    text = OpenMetricsSink(aggregator=aggregator)
    written = JsonMetricsSink(_Collector(), aggregator=aggregator)
    register_catalog(text)
    text.increment("oktografx_database_opens_total")
    document = json.loads(written.document())
    entry = next(item for item in document["metrics"] if item["name"] == "oktografx_database_opens_total")
    assert entry["samples"] == [{"labels": {}, "value": 1.0}]
    assert written.snapshot() == text.snapshot()


def test_the_timing_context_manager_reaches_the_document() -> None:
    sink = JsonMetricsSink(_Collector())
    sink.register(PROBE_HISTOGRAM)
    with sink.time("oktografx_probe_seconds"):
        pass
    assert sink.sample_value("oktografx_probe_seconds") == 1.0


def test_an_indented_document_is_still_valid_json() -> None:
    sink = JsonMetricsSink(_Collector(), indent=2)
    register_catalog(sink)
    document = sink.document()
    assert "\n  " in document
    assert json.loads(document)["version"] == DOCUMENT_VERSION


def test_a_destination_that_raises_is_reported_as_a_configuration_error() -> None:
    def broken(document: str) -> None:
        raise RuntimeError("the disk is on fire")

    sink = JsonMetricsSink(broken)
    with pytest.raises(GrafxConfigurationError, match="refused the document"):
        sink.publish()


def test_the_file_writer_appends_one_line_per_document(tmp_path: Path) -> None:
    destination = tmp_path / "metrics.json"
    writer = RotatingFileWriter(str(destination))
    assert writer.path == str(destination)
    sink = JsonMetricsSink(writer)
    register_catalog(sink)
    sink.increment("oktografx_database_opens_total")
    sink.publish()
    sink.increment("oktografx_database_opens_total")
    sink.publish()

    lines = destination.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first, second = (json.loads(line) for line in lines)
    assert _value(first, "oktografx_database_opens_total") == 1.0
    assert _value(second, "oktografx_database_opens_total") == 2.0


def _value(document: dict[str, object], name: str) -> float:
    entry = next(item for item in document["metrics"] if item["name"] == name)  # type: ignore[index]
    return float(entry["samples"][0]["value"])


def test_the_file_writer_rotates_when_it_passes_the_bound(tmp_path: Path) -> None:
    destination = tmp_path / "metrics.json"
    writer = RotatingFileWriter(str(destination), max_bytes=64, backups=2)
    writer("a" * 100)
    assert not destination.exists()
    assert (tmp_path / "metrics.json.1").read_text(encoding="utf-8").startswith("a")

    writer("b" * 100)
    assert (tmp_path / "metrics.json.1").read_text(encoding="utf-8").startswith("b")
    assert (tmp_path / "metrics.json.2").read_text(encoding="utf-8").startswith("a")

    writer("c" * 100)
    assert (tmp_path / "metrics.json.1").read_text(encoding="utf-8").startswith("c")
    assert (tmp_path / "metrics.json.2").read_text(encoding="utf-8").startswith("b")
    assert not (tmp_path / "metrics.json.3").exists()


def test_the_file_writer_can_keep_no_backup_at_all(tmp_path: Path) -> None:
    destination = tmp_path / "metrics.json"
    writer = RotatingFileWriter(str(destination), max_bytes=16, backups=0)
    writer("x" * 40)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_the_file_writer_adds_the_line_break_only_when_it_is_missing(tmp_path: Path) -> None:
    destination = tmp_path / "metrics.json"
    writer = RotatingFileWriter(str(destination))
    writer("{}\n")
    writer("{}")
    assert destination.read_text(encoding="utf-8") == "{}\n{}\n"


def test_the_file_writer_refuses_an_unusable_configuration(tmp_path: Path) -> None:
    with pytest.raises(GrafxConfigurationError, match="destination path"):
        RotatingFileWriter("")
    with pytest.raises(GrafxConfigurationError, match="positive size bound"):
        RotatingFileWriter(str(tmp_path / "metrics.json"), max_bytes=0)
    with pytest.raises(GrafxConfigurationError, match="negative number of backups"):
        RotatingFileWriter(str(tmp_path / "metrics.json"), backups=-1)


def test_the_file_writer_reports_an_unreachable_destination(tmp_path: Path) -> None:
    unreachable = tmp_path / "missing-directory" / "metrics.json"
    writer = RotatingFileWriter(str(unreachable))
    with pytest.raises(GrafxConfigurationError, match="could not write"):
        writer("{}")
