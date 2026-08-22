"""Aggregation and text exposition (SPEC-M1 FR-14, TR-7, BR-12, OR-6, IR-3; api_c5e17ca5).

Two halves. The first is the exposition format itself, pinned with golden bodies so a change in
rendering is a deliberate act rather than a surprise for a scraper. The second is the
registration contract: TR-7 says the bound of a label is declared once and checked there, and the
tests below take that to its end by proving that every way of contradicting a declaration is
refused with a GrafxConfigurationError.
"""

from __future__ import annotations

import threading
from types import MappingProxyType

import pytest

from okto_grafx.adapters.metrics_openmetrics import (
    CONTENT_TYPE,
    FREE_FORM_LABEL_ALPHABET,
    FREE_FORM_LABEL_MAX_LENGTH,
    MetricAggregator,
    OpenMetricsSink,
    escape_help,
    escape_label_value,
    format_number,
)
from okto_grafx.adapters.metrics_json import JsonMetricsSink
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
)
from okto_grafx.domain.ports.metrics import LabelSpec, MetricDescriptor, MetricKind
from okto_grafx.engine.metrics_catalog import metric, register_catalog

PROBE_COUNTER = MetricDescriptor(
    name="oktografx_probe_total",
    kind=MetricKind.COUNTER,
    description="A probe counter.",
)
PROBE_GAUGE = MetricDescriptor(
    name="oktografx_probe_bytes",
    kind=MetricKind.GAUGE,
    description="A probe gauge.",
    unit="bytes",
)
PROBE_HISTOGRAM = MetricDescriptor(
    name="oktografx_probe_seconds",
    kind=MetricKind.HISTOGRAM,
    description="A probe histogram.",
    unit="seconds",
    buckets=(1.0, 2.0),
)
PROBE_LABELLED_HISTOGRAM = MetricDescriptor(
    name="oktografx_probe_backlog",
    kind=MetricKind.HISTOGRAM,
    description="A probe histogram with a free-form bounded label.",
    unit="entries",
    labels=(LabelSpec(name="space", allowed_values=None, max_cardinality=3),),
    buckets=(1.0, 2.0),
)
HOSTILE_QUOTED: str = 'quote " backslash \\ end'
HOSTILE_NEWLINE: str = "line\nbreak"
HOSTILE_CARRIAGE_RETURN: str = "carriage\rreturn"
"""Values an enumerated label may legitimately declare, and the renderer must survive."""

PROBE_TWO_LABELS = MetricDescriptor(
    name="oktografx_probe_backlog",
    kind=MetricKind.GAUGE,
    description="A probe gauge with one free-form label and one enumerated label.",
    unit="entries",
    labels=(
        LabelSpec(name="space", allowed_values=None, max_cardinality=3),
        LabelSpec(name="kind", allowed_values=frozenset({"page", "record"}), max_cardinality=2),
    ),
)
"""One label with a budget and one without, which is what makes a spent slot observable."""


PROBE_HOSTILE_LABEL = MetricDescriptor(
    name="oktografx_probe_depth",
    kind=MetricKind.GAUGE,
    description="A probe gauge whose enumerated label carries exposition metacharacters.",
    unit="entries",
    labels=(
        LabelSpec(
            name="probe",
            allowed_values=frozenset(
                {HOSTILE_QUOTED, HOSTILE_NEWLINE, HOSTILE_CARRIAGE_RETURN}
            ),
            max_cardinality=3,
        ),
    ),
)
PROBE_LABELLED = MetricDescriptor(
    name="oktografx_probe_entries",
    kind=MetricKind.GAUGE,
    description="A probe gauge with a free-form bounded label.",
    unit="entries",
    labels=(LabelSpec(name="space", allowed_values=None, max_cardinality=3),),
)


def _sink(*descriptors: MetricDescriptor) -> OpenMetricsSink:
    sink = OpenMetricsSink()
    for descriptor in descriptors:
        sink.register(descriptor)
    return sink


# --- exposition format -------------------------------------------------------------------------


def test_an_empty_registry_renders_an_empty_body() -> None:
    assert OpenMetricsSink().render() == ""


def test_a_declared_metric_renders_its_header_even_with_no_samples() -> None:
    assert _sink(PROBE_COUNTER).render() == (
        "# HELP oktografx_probe_total A probe counter.\n"
        "# TYPE oktografx_probe_total counter\n"
    )


def test_a_counter_renders_as_a_single_series() -> None:
    sink = _sink(PROBE_COUNTER)
    sink.increment("oktografx_probe_total")
    sink.increment("oktografx_probe_total", 2.0)
    assert sink.render() == (
        "# HELP oktografx_probe_total A probe counter.\n"
        "# TYPE oktografx_probe_total counter\n"
        "oktografx_probe_total 3\n"
    )


def test_a_gauge_renders_its_last_value() -> None:
    sink = _sink(PROBE_GAUGE)
    sink.set_gauge("oktografx_probe_bytes", 4096.0)
    sink.set_gauge("oktografx_probe_bytes", 12.5)
    assert sink.render() == (
        "# HELP oktografx_probe_bytes A probe gauge.\n"
        "# TYPE oktografx_probe_bytes gauge\n"
        "oktografx_probe_bytes 12.5\n"
    )


def test_a_histogram_renders_cumulative_buckets_a_sum_and_a_count() -> None:
    sink = _sink(PROBE_HISTOGRAM)
    for observation in (0.5, 1.5, 4.5):
        sink.observe("oktografx_probe_seconds", observation)
    assert sink.render() == (
        "# HELP oktografx_probe_seconds A probe histogram.\n"
        "# TYPE oktografx_probe_seconds histogram\n"
        'oktografx_probe_seconds_bucket{le="1"} 1\n'
        'oktografx_probe_seconds_bucket{le="2"} 2\n'
        'oktografx_probe_seconds_bucket{le="+Inf"} 3\n'
        "oktografx_probe_seconds_sum 6.5\n"
        "oktografx_probe_seconds_count 3\n"
    )


def test_a_histogram_counts_an_observation_equal_to_a_boundary_in_that_bucket() -> None:
    sink = _sink(PROBE_HISTOGRAM)
    sink.observe("oktografx_probe_seconds", 1.0)
    body = sink.render()
    assert 'oktografx_probe_seconds_bucket{le="1"} 1\n' in body
    assert 'oktografx_probe_seconds_bucket{le="2"} 1\n' in body


def test_a_histogram_with_labels_repeats_them_on_every_series() -> None:
    sink = _sink(metric("oktografx_fsync_duration_seconds"))
    sink.observe("oktografx_fsync_duration_seconds", 0.002, {"target": "wal"})
    body = sink.render()
    assert 'oktografx_fsync_duration_seconds_bucket{target="wal",le="0.0025"} 1\n' in body
    assert 'oktografx_fsync_duration_seconds_sum{target="wal"} 0.002\n' in body
    assert 'oktografx_fsync_duration_seconds_count{target="wal"} 1\n' in body


def test_labels_are_rendered_in_the_order_the_descriptor_declares_them() -> None:
    sink = _sink(metric("oktografx_vector_query_latency_seconds"))
    sink.observe(
        "oktografx_vector_query_latency_seconds",
        0.01,
        {"phase": "traverse", "regime": "approximate"},
    )
    assert 'regime="approximate",phase="traverse",le=' in sink.render()


def test_a_label_value_is_escaped() -> None:
    """Escaping is proved on an enumerated label, the only kind that can carry these.

    A free-form label is constrained to a hash or a catalog name (A79), so a quote or a
    newline cannot reach the renderer through one. An enumerated label can: its domain is
    declared in code by the component that owns the metric, and whatever it declares has to
    survive the exposition grammar intact.
    """
    sink = _sink(PROBE_HOSTILE_LABEL)
    sink.set_gauge("oktografx_probe_depth", 1.0, {"probe": 'quote " backslash \\ end'})
    sink.set_gauge("oktografx_probe_depth", 2.0, {"probe": "line\nbreak"})
    body = sink.render()
    assert 'oktografx_probe_depth{probe="line\\nbreak"} 2\n' in body
    assert 'oktografx_probe_depth{probe="quote \\" backslash \\\\ end"} 1\n' in body
    # Four lines: two headers and two series. A raw newline inside a value would make five.
    assert len(body.splitlines()) == 4


def test_a_description_is_escaped_on_the_help_line() -> None:
    descriptor = MetricDescriptor(
        name="oktografx_probe_depth",
        kind=MetricKind.GAUGE,
        description="A backslash \\ and a newline\nstay on one line.",
        unit="entries",
    )
    body = _sink(descriptor).render()
    assert body.splitlines()[0] == (
        "# HELP oktografx_probe_depth A backslash \\\\ and a newline\\nstay on one line."
    )
    assert len(body.splitlines()) == 2


def test_the_escaping_helpers_are_exact() -> None:
    assert escape_label_value('a"b') == 'a\\"b'
    assert escape_label_value("a\\b") == "a\\\\b"
    assert escape_label_value("a\nb") == "a\\nb"
    assert escape_label_value("a\\\nb") == "a\\\\\\nb"
    assert escape_help('quotes " are not escaped in help') == 'quotes " are not escaped in help'
    assert escape_help("a\\b\nc") == "a\\\\b\\nc"


def test_numbers_render_the_way_a_parser_expects() -> None:
    assert format_number(1.0) == "1"
    assert format_number(0.0) == "0"
    assert format_number(-3.0) == "-3"
    assert format_number(0.0025) == "0.0025"
    assert format_number(0.00005) == "5e-05"
    assert format_number(float("inf")) == "+Inf"
    assert format_number(float("-inf")) == "-Inf"
    assert format_number(float("nan")) == "NaN"


def test_the_body_matches_the_frozen_api_contract_example() -> None:
    # api_c5e17ca5 pins this exact shape for the ledger depth family.
    sink = _sink(metric("oktografx_ledger_depth"))
    sink.set_gauge("oktografx_ledger_depth", 0, {"origin_class": "reapplicable"})
    sink.set_gauge("oktografx_ledger_depth", 0, {"origin_class": "forensic"})
    lines = sink.render().splitlines()
    assert lines == [
        "# HELP oktografx_ledger_depth Number of unapplied-work ledger entries by origin class.",
        "# TYPE oktografx_ledger_depth gauge",
        'oktografx_ledger_depth{origin_class="forensic"} 0',
        'oktografx_ledger_depth{origin_class="reapplicable"} 0',
    ]
    assert sink.content_type == CONTENT_TYPE == "text/plain; version=0.0.4"


def test_the_body_carries_no_openmetrics_only_syntax() -> None:
    # The frozen content type is the 0.0.4 text format, which has neither a trailer nor the
    # _created series of OpenMetrics 1.0; a 0.0.4 parser would reject both.
    sink = OpenMetricsSink()
    register_catalog(sink)
    sink.increment("oktografx_database_opens_total")
    body = sink.render()
    assert "# EOF" not in body
    assert "_created" not in body
    assert body.endswith("\n")
    assert all(line for line in body.splitlines())


def test_the_ordering_is_total_and_independent_of_call_order() -> None:
    first = _sink(PROBE_COUNTER, PROBE_LABELLED)
    second = _sink(PROBE_LABELLED, PROBE_COUNTER)
    for space in ("beta", "alpha"):
        first.set_gauge("oktografx_probe_entries", 1.0, {"space": space})
    for space in ("alpha", "beta"):
        second.set_gauge("oktografx_probe_entries", 1.0, {"space": space})
    first.increment("oktografx_probe_total")
    second.increment("oktografx_probe_total")
    assert first.render() == second.render()
    assert first.render().index("oktografx_probe_entries") < first.render().index(
        "oktografx_probe_total"
    )


def test_every_registered_metric_of_the_catalog_renders() -> None:
    sink = OpenMetricsSink()
    register_catalog(sink)
    body = sink.render()
    for descriptor in sink.aggregator.descriptors():
        assert f"# TYPE {descriptor.name} {descriptor.kind.value}\n" in body
        assert f"# HELP {descriptor.name} {descriptor.description}\n" in body


# --- registration is the authority ---------------------------------------------------------------


def test_an_unregistered_name_is_refused_on_every_entry_point() -> None:
    sink = OpenMetricsSink()
    for call in (
        lambda: sink.increment("oktografx_ghost_total"),
        lambda: sink.set_gauge("oktografx_ghost_total", 1.0),
        lambda: sink.observe("oktografx_ghost_total", 1.0),
    ):
        with pytest.raises(GrafxConfigurationError) as failure:
            call()
        assert "was never registered" in str(failure.value)
        assert isinstance(failure.value, GrafxError)


def test_a_timed_block_over_an_unregistered_name_is_refused_at_the_end() -> None:
    sink = OpenMetricsSink()
    with pytest.raises(GrafxConfigurationError):
        with sink.time("oktografx_ghost_seconds"):
            pass


def test_the_kind_of_a_metric_is_part_of_its_registration() -> None:
    sink = _sink(PROBE_COUNTER, PROBE_GAUGE, PROBE_HISTOGRAM)
    with pytest.raises(GrafxConfigurationError, match="cannot be used as a gauge"):
        sink.set_gauge("oktografx_probe_total", 1.0)
    with pytest.raises(GrafxConfigurationError, match="cannot be used as a counter"):
        sink.increment("oktografx_probe_bytes")
    with pytest.raises(GrafxConfigurationError, match="cannot be used as a histogram"):
        sink.observe("oktografx_probe_total", 1.0)
    with pytest.raises(GrafxConfigurationError, match="cannot be used as a counter"):
        sink.increment("oktografx_probe_seconds")


def test_a_label_the_descriptor_does_not_declare_is_refused() -> None:
    sink = _sink(metric("oktografx_ledger_depth"))
    with pytest.raises(GrafxConfigurationError, match="does not declare the label"):
        sink.set_gauge(
            "oktografx_ledger_depth", 1.0, {"origin_class": "forensic", "shard": "seven"}
        )


def test_a_label_set_of_the_right_size_but_the_wrong_names_is_refused() -> None:
    # The length check passes on one-for-one substitution, so this branch is the only guard for
    # a call that swaps a declared label for an undeclared one.
    sink = _sink(metric("oktografx_ledger_depth"))
    with pytest.raises(GrafxConfigurationError) as failure:
        sink.set_gauge("oktografx_ledger_depth", 1.0, {"shard": "seven"})
    # Both halves, because a caller told only that something is missing goes looking for the
    # wrong defect: they did pass one label, it is simply not a label of this metric.
    assert "does not declare the label(s) 'shard'" in str(failure.value)
    assert "requires the label(s) 'origin_class'" in str(failure.value)
    assert failure.value.details["value"] == {"unknown": ["shard"], "missing": ["origin_class"]}

    with pytest.raises(GrafxConfigurationError, match="does not declare the label"):
        sink.set_gauge("oktografx_ledger_depth", 1.0, {"origin_clas": "forensic"})

    # A two-label metric with two wrong names is the same shape one size up.
    latency = _sink(metric("oktografx_vector_query_latency_seconds"))
    with pytest.raises(GrafxConfigurationError) as failure:
        latency.observe(
            "oktografx_vector_query_latency_seconds", 0.1, {"mode": "read", "target": "wal"}
        )
    assert "'mode', 'target'" in str(failure.value)
    assert "'phase', 'regime'" in str(failure.value)

    assert sink.sample_value("oktografx_ledger_depth", {"origin_class": "forensic"}) is None


def test_a_missing_label_is_refused() -> None:
    sink = _sink(metric("oktografx_ledger_depth"))
    with pytest.raises(GrafxConfigurationError, match="requires the label"):
        sink.set_gauge("oktografx_ledger_depth", 1.0)
    with pytest.raises(GrafxConfigurationError, match="requires the label"):
        sink.set_gauge("oktografx_ledger_depth", 1.0, {})


def test_a_label_on_a_metric_that_declares_none_is_refused() -> None:
    sink = _sink(PROBE_COUNTER)
    with pytest.raises(GrafxConfigurationError, match="does not declare the label"):
        sink.increment("oktografx_probe_total", 1.0, {"outcome": "granted"})


def test_a_label_value_outside_the_declared_domain_is_refused() -> None:
    sink = _sink(metric("oktografx_lease_wait_seconds"))
    with pytest.raises(GrafxConfigurationError, match="is not one of them"):
        sink.observe("oktografx_lease_wait_seconds", 0.1, {"outcome": "stolen"})
    sink.observe("oktografx_lease_wait_seconds", 0.1, {"outcome": "granted"})


def test_a_label_value_that_is_not_a_non_empty_string_is_refused() -> None:
    sink = _sink(PROBE_LABELLED)
    with pytest.raises(GrafxConfigurationError, match="non-empty string"):
        sink.set_gauge("oktografx_probe_entries", 1.0, {"space": ""})
    with pytest.raises(GrafxConfigurationError, match="non-empty string"):
        sink.set_gauge("oktografx_probe_entries", 1.0, {"space": 7})  # type: ignore[dict-item]


def test_exceeding_the_declared_cardinality_of_a_label_is_refused() -> None:
    # This is the only place the bound of an unenumerated label such as db or space can be
    # enforced at all, and TR-7 is the reason it must be enforced somewhere.
    sink = _sink(PROBE_LABELLED)
    for index in range(3):
        sink.set_gauge("oktografx_probe_entries", 1.0, {"space": f"space-{index}"})
    with pytest.raises(GrafxConfigurationError, match="one too many"):
        sink.set_gauge("oktografx_probe_entries", 1.0, {"space": "space-3"})
    # A value already seen still works: the bound is on distinct values, not on calls.
    sink.set_gauge("oktografx_probe_entries", 2.0, {"space": "space-0"})
    assert sink.sample_value("oktografx_probe_entries", {"space": "space-0"}) == 2.0


QUOTE: str = chr(34)
BACKSLASH: str = chr(92)
NEWLINE: str = chr(10)
RETURN: str = chr(13)
"""Named so a test can carry an exposition metacharacter without escaping it inline."""

ACCEPTED_FREE_FORM_VALUES: tuple[str, ...] = (
    "minilm-v2",
    "a3f9c1e2b7d40518",
    "warehouse.main",
    "space_1",
    "A" * 64,
    "0",
)
"""What CONTRACT.md section 9 promises these labels carry: a short hash or a catalog name."""

REFUSED_FREE_FORM_VALUES: tuple[tuple[str, str], ...] = (
    ("posix path", "/var/lib/okto/data"),
    ("windows path", "C:" + BACKSLASH + "data" + BACKSLASH + "grafx.db"),
    ("relative path", "../../etc/passwd"),
    ("url", "http://host/db"),
    ("home directory", "~jpamb/projects/okto"),
    ("sentence", "the primary warehouse database"),
    ("exposition brace", "name{label=1}"),
    ("exposition quote", "q" + QUOTE + "uote"),
    ("exposition comma", "one,two"),
    ("newline", "line" + NEWLINE + "break"),
    ("carriage return", "line" + RETURN + "break"),
    ("comment", "value #tag"),
    ("too long", "x" * 65),
    ("unicode", "caf" + chr(233)),
    ("colon", "host:5432"),
)
"""Everything section 9 says these labels never carry, refused by the alphabet, not by name.

The list is illustrative, not a specification. The rule is the closed alphabet: not one of these
had to be predicted for it to be refused, which is the point of stating the constraint
positively instead of enumerating the shapes to reject.
"""


@pytest.mark.parametrize("value", ACCEPTED_FREE_FORM_VALUES, ids=ACCEPTED_FREE_FORM_VALUES)
def test_a_free_form_label_accepts_a_hash_or_a_catalog_name(value: str) -> None:
    sink = _sink(metric("oktografx_vector_index_entries"))
    sink.set_gauge("oktografx_vector_index_entries", 1.0, {"space": value})
    assert sink.sample_value("oktografx_vector_index_entries", {"space": value}) == 1.0


@pytest.mark.parametrize(
    ("label", "value"), REFUSED_FREE_FORM_VALUES, ids=[row[0] for row in REFUSED_FREE_FORM_VALUES]
)
def test_a_free_form_label_refuses_a_path_or_free_text(label: str, value: str) -> None:
    sink = _sink(metric("oktografx_vector_index_entries"))
    with pytest.raises(GrafxConfigurationError) as failure:
        sink.set_gauge("oktografx_vector_index_entries", 1.0, {"space": value})
    assert "short hash or a catalog name" in str(failure.value)


@pytest.mark.parametrize(
    ("label", "value"), REFUSED_FREE_FORM_VALUES, ids=[row[0] for row in REFUSED_FREE_FORM_VALUES]
)
def test_a_refused_free_form_value_leaves_no_residue(label: str, value: str) -> None:
    # The same guarantee the cardinality bound gives: a refusal creates no series and spends no
    # part of the budget, so the refusal is reproducible rather than one-shot.
    sink = _sink(metric("oktografx_vector_index_entries"))
    for index in range(60):
        sink.set_gauge("oktografx_vector_index_entries", 1.0, {"space": f"space-{index}"})

    with pytest.raises(GrafxConfigurationError):
        sink.set_gauge("oktografx_vector_index_entries", 1.0, {"space": value})
    with pytest.raises(GrafxConfigurationError):
        sink.set_gauge("oktografx_vector_index_entries", 1.0, {"space": value})

    assert value not in sink.render()
    assert len(sink.snapshot()["oktografx_vector_index_entries"]["samples"]) == 60
    # The four slots the refusals did not take are still there to be used.
    for index in range(60, 64):
        sink.set_gauge("oktografx_vector_index_entries", 1.0, {"space": f"space-{index}"})
    assert len(sink.snapshot()["oktografx_vector_index_entries"]["samples"]) == 64


def test_the_free_form_constraint_is_exactly_the_pinned_alphabet_and_length() -> None:
    # A56/A68: pin the literals, not an expression that slides with them.
    assert FREE_FORM_LABEL_MAX_LENGTH == 64
    assert FREE_FORM_LABEL_ALPHABET == frozenset(
        "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    )
    assert len(FREE_FORM_LABEL_ALPHABET) == 65
    for forbidden in (
        " ", "/", BACKSLASH, ":", "{", "}", ",", QUOTE, NEWLINE, RETURN, "#", "~", "=",
    ):
        assert forbidden not in FREE_FORM_LABEL_ALPHABET


def test_the_constraint_applies_only_where_the_domain_is_open() -> None:
    # An enumerated label is closed-world: its values are declared in code by the component that
    # owns the metric, so the alphabet has no business filtering them.
    sink = _sink(PROBE_HOSTILE_LABEL)
    sink.set_gauge("oktografx_probe_depth", 1.0, {"probe": HOSTILE_NEWLINE})
    assert sink.sample_value("oktografx_probe_depth", {"probe": HOSTILE_NEWLINE}) == 1.0


def test_a_multi_label_refusal_does_not_count_the_labels_that_passed() -> None:
    """The first label is acceptable and the second is not; neither may be counted.

    Observing that no series appeared is not enough, and neither are two enumerated labels: an
    enumerated label has no budget to spend, so a slot taken by the label that passed would be
    invisible either way. The metric here carries a free-form label with a budget, and the test
    spends the budget down to its last slot first, so a value recorded by a refused emission
    shows up as a refusal that should have succeeded.
    """
    sink = _sink(PROBE_TWO_LABELS)
    for index in range(2):
        sink.set_gauge("oktografx_probe_backlog", 1.0, {"space": f"space-{index}", "kind": "page"})
    # One slot of the three left. A refusal must not consume it.
    with pytest.raises(GrafxConfigurationError, match="is not one of them"):
        sink.set_gauge(
            "oktografx_probe_backlog", 1.0, {"space": "space-2", "kind": "not-a-kind"}
        )
    with pytest.raises(GrafxConfigurationError, match="is not one of them"):
        sink.set_gauge(
            "oktografx_probe_backlog", 1.0, {"space": "space-3", "kind": "not-a-kind"}
        )

    # The slot is still there, and it is the refused value's own that proves it: if either
    # refusal had recorded "space-2", this call would be refused as one too many.
    sink.set_gauge("oktografx_probe_backlog", 1.0, {"space": "space-2", "kind": "page"})
    assert sink.sample_value("oktografx_probe_backlog", {"space": "space-2", "kind": "page"}) == 1.0
    with pytest.raises(GrafxConfigurationError, match="one too many"):
        sink.set_gauge("oktografx_probe_backlog", 1.0, {"space": "space-9", "kind": "page"})


def test_a_refusal_on_the_first_label_does_not_count_the_second_either() -> None:
    sink = _sink(PROBE_TWO_LABELS)
    for index in range(2):
        sink.set_gauge("oktografx_probe_backlog", 1.0, {"space": f"space-{index}", "kind": "page"})
    with pytest.raises(GrafxConfigurationError):
        sink.set_gauge("oktografx_probe_backlog", 1.0, {"space": "a b", "kind": "record"})
    # "record" is a legal value of the enumerated label and must not have been consumed either;
    # the free-form slot is likewise untouched.
    sink.set_gauge("oktografx_probe_backlog", 1.0, {"space": "space-2", "kind": "record"})
    assert (
        sink.sample_value("oktografx_probe_backlog", {"space": "space-2", "kind": "record"}) == 1.0
    )


def test_a_repeated_registration_of_the_same_descriptor_is_accepted() -> None:
    sink = OpenMetricsSink()
    sink.register(PROBE_COUNTER)
    sink.register(PROBE_COUNTER)
    sink.register(
        MetricDescriptor(
            name="oktografx_probe_total", kind=MetricKind.COUNTER, description="A probe counter."
        )
    )
    assert len(sink.aggregator.descriptors()) == 1


def test_registering_a_different_declaration_under_the_same_name_is_refused() -> None:
    sink = OpenMetricsSink()
    sink.register(PROBE_COUNTER)
    with pytest.raises(GrafxConfigurationError, match="already registered"):
        sink.register(
            MetricDescriptor(
                name="oktografx_probe_total",
                kind=MetricKind.COUNTER,
                description="A different probe counter.",
            )
        )


def test_registering_something_that_is_not_a_descriptor_is_refused() -> None:
    with pytest.raises(GrafxConfigurationError, match="needs a MetricDescriptor"):
        OpenMetricsSink().register("oktografx_probe_total")  # type: ignore[arg-type]


def test_a_counter_refuses_a_negative_increment() -> None:
    sink = _sink(PROBE_COUNTER)
    with pytest.raises(GrafxConfigurationError, match="negative amount"):
        sink.increment("oktografx_probe_total", -1.0)


def test_a_value_that_is_not_a_finite_number_is_refused() -> None:
    sink = _sink(PROBE_GAUGE, PROBE_HISTOGRAM)
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(GrafxConfigurationError, match="finite value"):
            sink.set_gauge("oktografx_probe_bytes", bad)
    for bad_type in ("12", None, True):
        with pytest.raises(GrafxConfigurationError, match="numeric value"):
            sink.observe("oktografx_probe_seconds", bad_type)  # type: ignore[arg-type]


def test_an_integer_value_is_accepted_and_kept_exact() -> None:
    sink = _sink(PROBE_GAUGE)
    sink.set_gauge("oktografx_probe_bytes", 8192)
    assert sink.sample_value("oktografx_probe_bytes") == 8192.0


# --- reading the state ---------------------------------------------------------------------------


def test_the_snapshot_is_machine_readable_and_immutable() -> None:
    sink = OpenMetricsSink()
    register_catalog(sink)
    sink.set_gauge("oktografx_vector_recall_ratio", 0.93)
    snapshot = sink.snapshot()
    recall = snapshot["oktografx_vector_recall_ratio"]
    assert recall["kind"] == "gauge"
    assert recall["unit"] == "ratio"
    assert recall["samples"][0]["value"] == 0.93
    assert recall["samples"][0]["labels"] == {}
    with pytest.raises(TypeError):
        snapshot["oktografx_vector_recall_ratio"] = None  # type: ignore[index]
    with pytest.raises(TypeError):
        recall["kind"] = "counter"  # type: ignore[index]


def test_the_snapshot_of_a_histogram_carries_its_buckets() -> None:
    sink = _sink(PROBE_HISTOGRAM)
    for observation in (0.5, 1.5, 4.5):
        sink.observe("oktografx_probe_seconds", observation)
    sample = sink.snapshot()["oktografx_probe_seconds"]["samples"][0]
    assert sample["count"] == 3.0
    assert sample["sum"] == 6.5
    assert dict(sample["buckets"]) == {"1": 1.0, "2": 2.0, "+Inf": 3.0}


def test_sample_value_answers_for_each_kind_and_is_none_before_the_first_touch() -> None:
    sink = OpenMetricsSink()
    register_catalog(sink)
    assert sink.sample_value("oktografx_vector_recall_ratio") is None
    sink.set_gauge("oktografx_vector_recall_ratio", 0.9)
    assert sink.sample_value("oktografx_vector_recall_ratio") == 0.9
    sink.increment("oktografx_database_opens_total", 2.0)
    assert sink.sample_value("oktografx_database_opens_total") == 2.0
    sink.observe("oktografx_lease_wait_seconds", 0.1, {"outcome": "granted"})
    assert sink.sample_value("oktografx_lease_wait_seconds", {"outcome": "granted"}) == 1.0
    assert sink.sample_value("oktografx_lease_wait_seconds", {"outcome": "timeout"}) is None


def test_reading_a_series_never_spends_the_cardinality_budget() -> None:
    """sample_value observes; it does not create. This is the path a CI gate takes.

    Recording the label value while reading it would mean that asking about a series the engine
    has not touched consumes one of the declared slots of an unenumerated label, and that at the
    bound the read raises GrafxConfigurationError instead of answering None -- on exactly the
    call AC-14 and SPEC-VEC AC-10 build a release gate out of.
    """
    sink = _sink(PROBE_LABELLED)
    for index in range(3):
        sink.set_gauge("oktografx_probe_entries", 1.0, {"space": f"space-{index}"})
    # The budget is now full: writing a fourth value is refused.
    with pytest.raises(GrafxConfigurationError, match="one too many"):
        sink.set_gauge("oktografx_probe_entries", 1.0, {"space": "space-3"})

    # Reading that same untouched series answers None rather than raising...
    assert sink.sample_value("oktografx_probe_entries", {"space": "space-3"}) is None
    assert sink.sample_value("oktografx_probe_entries", {"space": "never-seen"}) is None

    # ...and having read it, the budget is exactly where it was: the three recorded series are
    # still there and no fourth has appeared.
    assert len(sink.snapshot()["oktografx_probe_entries"]["samples"]) == 3
    assert sink.sample_value("oktografx_probe_entries", {"space": "space-0"}) == 1.0


def test_reading_below_the_bound_does_not_consume_a_slot_either() -> None:
    # The same property one step from the bound, where a spent slot is visible as a refusal
    # that should not have happened.
    sink = _sink(PROBE_LABELLED)
    sink.set_gauge("oktografx_probe_entries", 1.0, {"space": "first"})
    for probe in ("second", "third", "fourth", "fifth"):
        assert sink.sample_value("oktografx_probe_entries", {"space": probe}) is None
    # Two writes must still be available: the reads took none of them.
    sink.set_gauge("oktografx_probe_entries", 2.0, {"space": "second"})
    sink.set_gauge("oktografx_probe_entries", 3.0, {"space": "third"})
    assert sink.sample_value("oktografx_probe_entries", {"space": "third"}) == 3.0


def test_sample_value_refuses_an_unregistered_name() -> None:
    with pytest.raises(GrafxConfigurationError):
        OpenMetricsSink().sample_value("oktografx_ghost_total")


def test_reset_clears_the_numbers_and_keeps_the_declarations() -> None:
    sink = _sink(PROBE_COUNTER)
    sink.increment("oktografx_probe_total")
    sink.aggregator.reset()
    assert sink.sample_value("oktografx_probe_total") is None
    assert sink.aggregator.is_registered("oktografx_probe_total")
    sink.increment("oktografx_probe_total")
    assert sink.sample_value("oktografx_probe_total") == 1.0


def test_reset_returns_the_cardinality_budget_as_well_as_the_numbers() -> None:
    # A reset that dropped the samples but kept the observed label values would leave the budget
    # of an unenumerated label spent: the series are gone and yet no new one may be created.
    sink = _sink(PROBE_LABELLED)
    for index in range(3):
        sink.set_gauge("oktografx_probe_entries", 1.0, {"space": f"space-{index}"})
    with pytest.raises(GrafxConfigurationError, match="one too many"):
        sink.set_gauge("oktografx_probe_entries", 1.0, {"space": "space-3"})

    sink.aggregator.reset()

    for index in range(3, 6):
        sink.set_gauge("oktografx_probe_entries", 1.0, {"space": f"space-{index}"})
    assert sink.sample_value("oktografx_probe_entries", {"space": "space-5"}) == 1.0
    # And the bound is back in force, not simply removed.
    with pytest.raises(GrafxConfigurationError, match="one too many"):
        sink.set_gauge("oktografx_probe_entries", 1.0, {"space": "space-6"})


def test_the_timing_context_manager_records_a_finite_duration() -> None:
    sink = _sink(PROBE_HISTOGRAM)
    with sink.time("oktografx_probe_seconds"):
        pass
    assert sink.sample_value("oktografx_probe_seconds") == 1.0
    sample = sink.snapshot()["oktografx_probe_seconds"]["samples"][0]
    assert 0.0 <= sample["sum"] < 5.0


def test_the_timing_context_manager_records_even_when_the_block_fails() -> None:
    """A block that raises is exactly the block whose duration matters most.

    The observation lives in a finally, so a durability barrier that failed slowly still lands
    in the histogram. The mirror of the rule proved elsewhere in this file: the exit emission
    may neither replace the error of the block nor be skipped because of it.
    """
    sink = _sink(PROBE_HISTOGRAM)
    raised = ValueError("propagated")
    with pytest.raises(ValueError) as failure:
        with sink.time("oktografx_probe_seconds"):
            raise raised

    # (a) the exception arrives untouched
    assert failure.value is raised
    assert failure.value.__context__ is None
    assert failure.value.__cause__ is None
    assert failure.value.__suppress_context__ is False

    # (b) the duration was recorded all the same
    assert sink.sample_value("oktografx_probe_seconds") == 1.0
    sample = sink.snapshot()["oktografx_probe_seconds"]["samples"][0]
    assert sample["count"] == 1.0
    assert sample["sum"] > 0.0


def test_a_failing_block_is_timed_with_labels_too() -> None:
    sink = _sink(metric("oktografx_fsync_duration_seconds"))
    with pytest.raises(GrafxCorruptionDetected):
        with sink.time("oktografx_fsync_duration_seconds", {"target": "wal"}):
            raise GrafxCorruptionDetected("The barrier failed.", file="wal")
    assert sink.sample_value("oktografx_fsync_duration_seconds", {"target": "wal"}) == 1.0
    sample = sink.snapshot()["oktografx_fsync_duration_seconds"]["samples"][0]
    assert sample["sum"] > 0.0
    assert sample["labels"] == {"target": "wal"}


def test_a_failing_block_is_timed_by_the_json_sink_too() -> None:
    # The JSON sink shares the timing code, and sharing it is only worth anything if it is
    # asserted rather than assumed.
    sink = JsonMetricsSink(lambda document: None)
    sink.register(PROBE_HISTOGRAM)
    with pytest.raises(ValueError):
        with sink.time("oktografx_probe_seconds"):
            raise ValueError("propagated")
    assert sink.sample_value("oktografx_probe_seconds") == 1.0
    assert sink.snapshot()["oktografx_probe_seconds"]["samples"][0]["sum"] > 0.0


# --- disabled sink and shared aggregator ----------------------------------------------------------


def test_a_disabled_sink_keeps_its_declarations_and_records_nothing() -> None:
    sink = OpenMetricsSink(enabled=False)
    register_catalog(sink)
    assert sink.enabled is False
    sink.increment("oktografx_database_opens_total")
    sink.set_gauge("oktografx_wal_size_bytes", 1.0)
    sink.observe("oktografx_lease_wait_seconds", 0.1, {"outcome": "granted"})
    with sink.time("oktografx_lease_wait_seconds", {"outcome": "granted"}):
        pass
    assert sink.sample_value("oktografx_database_opens_total") is None
    assert "# TYPE oktografx_database_opens_total counter\n" in sink.render()
    assert "\noktografx_database_opens_total " not in sink.render()


def test_two_sinks_can_share_one_aggregator() -> None:
    aggregator = MetricAggregator()
    first = OpenMetricsSink(aggregator=aggregator)
    second = OpenMetricsSink(aggregator=aggregator)
    first.register(PROBE_COUNTER)
    second.increment("oktografx_probe_total", 5.0)
    assert first.sample_value("oktografx_probe_total") == 5.0
    assert first.render() == second.render()


# --- concurrency ----------------------------------------------------------------------------------


def test_the_scraping_thread_reads_while_the_engine_writes() -> None:
    sink = _sink(PROBE_COUNTER, PROBE_HISTOGRAM)
    writers = 8
    per_writer = 500
    start = threading.Barrier(writers + 1)
    failures: list[BaseException] = []
    stop = threading.Event()

    def write() -> None:
        try:
            start.wait(timeout=5.0)
            for _ in range(per_writer):
                sink.increment("oktografx_probe_total")
                sink.observe("oktografx_probe_seconds", 0.5)
        except BaseException as failure:  # pragma: no cover - reported through the assertion
            failures.append(failure)

    def scrape() -> None:
        try:
            start.wait(timeout=5.0)
            while not stop.is_set():
                body = sink.render()
                assert body.startswith("# HELP ")
        except BaseException as failure:  # pragma: no cover - reported through the assertion
            failures.append(failure)

    threads = [threading.Thread(target=write, name=f"writer-{index}") for index in range(writers)]
    scraper = threading.Thread(target=scrape, name="scraper")
    scraper.start()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
    stop.set()
    scraper.join(timeout=10.0)

    assert failures == []
    assert sink.sample_value("oktografx_probe_total") == float(writers * per_writer)
    assert sink.sample_value("oktografx_probe_seconds") == float(writers * per_writer)
    assert not scraper.is_alive()


# --- a measurement never becomes the outcome of what it measured ---------------------------------


class _RecordingEvents:
    """An EventSink that keeps what it was told, so a swallowed failure is still observable."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def emit(self, event: str, payload: dict[str, object]) -> None:
        self.events.append((event, dict(payload)))


class _BrokenOnExit(MetricAggregator):
    """An aggregator whose observe() always fails, which is the only way to reach the finally."""

    def __init__(self, failure: BaseException | None = None) -> None:
        super().__init__()
        self._failure = failure or GrafxConfigurationError(
            "The sink lost the series.", field="name", value="probe"
        )

    def observe(self, name: str, value: float, labels: object = None) -> None:
        raise self._failure


def test_the_timer_refuses_a_wrong_kind_on_entry_and_never_runs_the_block() -> None:
    # A counter timed as a histogram is a programming error, and the place to say so is the
    # entry of the block, where nothing has happened yet.
    sink = _sink(PROBE_COUNTER)
    with pytest.raises(GrafxConfigurationError, match="cannot be used as a histogram"):
        with sink.time("oktografx_probe_total"):
            pytest.fail("the timed block must not run when the declaration is wrong")


def test_the_timer_refuses_an_unknown_name_and_bad_labels_on_entry() -> None:
    sink = _sink(PROBE_HISTOGRAM, metric("oktografx_fsync_duration_seconds"))
    with pytest.raises(GrafxConfigurationError, match="was never registered"):
        with sink.time("oktografx_ghost_seconds"):
            pytest.fail("the timed block must not run")
    with pytest.raises(GrafxConfigurationError, match="requires the label"):
        with sink.time("oktografx_fsync_duration_seconds"):
            pytest.fail("the timed block must not run")
    with pytest.raises(GrafxConfigurationError, match="is not one of them"):
        with sink.time("oktografx_fsync_duration_seconds", {"target": "index"}):
            pytest.fail("the timed block must not run")
    with pytest.raises(GrafxConfigurationError, match="does not declare the label"):
        with sink.time("oktografx_probe_seconds", {"target": "wal"}):
            pytest.fail("the timed block must not run")


def test_the_timer_refuses_a_cardinality_overflow_on_entry() -> None:
    sink = _sink(PROBE_LABELLED_HISTOGRAM)
    for index in range(3):
        with sink.time("oktografx_probe_backlog", {"space": f"space-{index}"}):
            pass
    with pytest.raises(GrafxConfigurationError, match="one too many"):
        with sink.time("oktografx_probe_backlog", {"space": "space-3"}):
            pytest.fail("the timed block must not run")


def test_an_error_raised_inside_a_timed_block_propagates_unchanged() -> None:
    sink = _sink(PROBE_HISTOGRAM)
    with pytest.raises(ValueError) as failure:
        with sink.time("oktografx_probe_seconds"):
            raise ValueError("the real application error")
    assert str(failure.value) == "the real application error"
    assert failure.value.__context__ is None
    assert sink.sample_value("oktografx_probe_seconds") == 1.0


def test_a_measurement_that_fails_at_exit_is_reported_and_swallowed() -> None:
    # The operational case behind this: a durability barrier that already succeeded must not be
    # turned into a raised error by the metric that was measuring it.
    events = _RecordingEvents()
    sink = OpenMetricsSink(aggregator=_BrokenOnExit(), events=events)
    sink.register(PROBE_HISTOGRAM)

    barrier_succeeded = False
    with sink.time("oktografx_probe_seconds"):
        barrier_succeeded = True
    assert barrier_succeeded is True
    assert events.events == [
        (
            "metrics.observation_failed",
            {"metric": "oktografx_probe_seconds", "error": "GrafxConfigurationError"},
        )
    ]


@pytest.mark.parametrize(
    "failure",
    [
        GrafxConfigurationError("The sink lost the series.", field="name", value="probe"),
        RuntimeError("something nobody predicted"),
        ValueError("a plain value error"),
        MemoryError("out of memory while recording"),
    ],
    ids=["grafx", "runtime", "value", "memory"],
)
def test_a_measurement_that_fails_at_exit_is_swallowed_whatever_it_raises(
    failure: BaseException,
) -> None:
    """The rule is about the outcome of the block, not about the class of the failure.

    A double that raises only the error the implementation happens to name encodes the symptom
    rather than the rule (A71): narrowing the handler to that one class would leave the test
    green while any other failure escaped and turned an operation that had already succeeded
    into a failure. So the double raises several, including one nobody would have predicted.
    """
    events = _RecordingEvents()
    sink = OpenMetricsSink(aggregator=_BrokenOnExit(failure), events=events)
    sink.register(PROBE_HISTOGRAM)

    completed = False
    with sink.time("oktografx_probe_seconds"):
        completed = True
    assert completed is True
    assert events.events == [
        (
            "metrics.observation_failed",
            {"metric": "oktografx_probe_seconds", "error": type(failure).__name__},
        )
    ]


@pytest.mark.parametrize(
    "failure",
    [RuntimeError("unpredicted"), ValueError("plain")],
    ids=["runtime", "value"],
)
def test_an_unpredicted_exit_failure_still_does_not_replace_the_block_error(
    failure: BaseException,
) -> None:
    sink = OpenMetricsSink(aggregator=_BrokenOnExit(failure))
    sink.register(PROBE_HISTOGRAM)
    raised = KeyError("the real application error")
    with pytest.raises(KeyError) as caught:
        with sink.time("oktografx_probe_seconds"):
            raise raised
    assert caught.value is raised


def test_a_measurement_that_fails_at_exit_does_not_replace_the_error_of_the_block() -> None:
    sink = OpenMetricsSink(aggregator=_BrokenOnExit())
    sink.register(PROBE_HISTOGRAM)
    with pytest.raises(ValueError, match="the real application error"):
        with sink.time("oktografx_probe_seconds"):
            raise ValueError("the real application error")


def test_a_broken_event_sink_cannot_turn_a_failed_measurement_into_an_outcome() -> None:
    class _BrokenEvents:
        def emit(self, event: str, payload: dict[str, object]) -> None:
            raise RuntimeError("the event sink is broken too")

    sink = OpenMetricsSink(aggregator=_BrokenOnExit(), events=_BrokenEvents())
    sink.register(PROBE_HISTOGRAM)
    with sink.time("oktografx_probe_seconds"):
        pass


def test_prepare_observation_is_the_entry_check_and_records_the_series_identity() -> None:
    aggregator = MetricAggregator()
    aggregator.register(PROBE_LABELLED_HISTOGRAM)
    aggregator.register(PROBE_COUNTER)
    aggregator.prepare_observation("oktografx_probe_backlog", {"space": "alpha"})
    # Nothing is measured yet, but the label value is now part of the declared cardinality.
    assert aggregator.sample_value("oktografx_probe_backlog", {"space": "alpha"}) is None
    aggregator.observe("oktografx_probe_backlog", 1.0, {"space": "alpha"})
    assert aggregator.sample_value("oktografx_probe_backlog", {"space": "alpha"}) == 1.0
    with pytest.raises(GrafxConfigurationError, match="cannot be used as a histogram"):
        aggregator.prepare_observation("oktografx_probe_total")


# --- wrong-typed arguments are refused with the typed error --------------------------------------


@pytest.mark.parametrize(
    "labels",
    [["origin_class"], ("origin_class",), "origin_class", 7, {"origin_class"}],
    ids=["list", "tuple", "str", "int", "set"],
)
def test_labels_that_are_not_a_mapping_are_refused_on_every_entry_point(labels: object) -> None:
    sink = OpenMetricsSink()
    register_catalog(sink)
    with pytest.raises(GrafxConfigurationError, match="labels as a mapping"):
        sink.set_gauge("oktografx_ledger_depth", 1.0, labels)  # type: ignore[arg-type]
    with pytest.raises(GrafxConfigurationError, match="labels as a mapping"):
        sink.increment("oktografx_checksum_failures_total", 1.0, labels)  # type: ignore[arg-type]
    with pytest.raises(GrafxConfigurationError, match="labels as a mapping"):
        sink.observe("oktografx_lease_wait_seconds", 1.0, labels)  # type: ignore[arg-type]
    with pytest.raises(GrafxConfigurationError, match="labels as a mapping"):
        with sink.time("oktografx_lease_wait_seconds", labels):  # type: ignore[arg-type]
            pytest.fail("the timed block must not run")
    with pytest.raises(GrafxConfigurationError, match="labels as a mapping"):
        sink.sample_value("oktografx_ledger_depth", labels)  # type: ignore[arg-type]


def test_a_mapping_that_is_not_a_dictionary_is_accepted() -> None:
    sink = _sink(metric("oktografx_ledger_depth"))
    sink.set_gauge("oktografx_ledger_depth", 2.0, MappingProxyType({"origin_class": "forensic"}))
    assert sink.sample_value("oktografx_ledger_depth", {"origin_class": "forensic"}) == 2.0
