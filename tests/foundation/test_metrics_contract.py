"""The metric naming and label contract (CONTRACT.md 4.4 and G7, SPEC-M1 TR-7).

A metric is rejected in the component that declares it, never in production, so every rejection
path below is part of the contract rather than a defensive extra.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.ports.metrics import (
    FORBIDDEN_LABEL_NAMES,
    METRIC_NAME_PREFIX,
    METRIC_UNIT_SUFFIXES,
    NON_EN_US_MARKERS,
    UNBOUNDED_LABEL_CARDINALITY_LIMIT,
    LabelSpec,
    MetricDescriptor,
    MetricKind,
)


def test_metric_kind_values() -> None:
    assert MetricKind.COUNTER.value == "counter"
    assert MetricKind.GAUGE.value == "gauge"
    assert MetricKind.HISTOGRAM.value == "histogram"
    assert MetricKind.COUNTER == "counter"


def test_forbidden_label_names_match_the_contract() -> None:
    assert FORBIDDEN_LABEL_NAMES == frozenset(
        {
            "node_id",
            "record_id",
            "id",
            "key",
            "path",
            "file",
            "query",
            "text",
            "message",
            "vector",
            "embedding",
            "uuid",
            "lsn",
            "offset",
        }
    )


# --- accepted descriptors -------------------------------------------------------------------


def test_a_counter_from_the_metric_catalog_is_accepted() -> None:
    descriptor = MetricDescriptor(
        name="oktografx_write_conflicts_total",
        kind=MetricKind.COUNTER,
        description="Commits refused by optimistic validation because partition sets intersect.",
    )
    assert descriptor.name.startswith(METRIC_NAME_PREFIX)
    assert descriptor.labels == ()
    assert descriptor.buckets == ()


def test_a_labelled_gauge_is_accepted() -> None:
    descriptor = MetricDescriptor(
        name="oktografx_active_transactions",
        kind=MetricKind.GAUGE,
        description="Transactions currently open, by mode.",
        labels=(LabelSpec(name="mode", allowed_values=frozenset({"read", "write"})),),
    )
    assert descriptor.labels[0].allowed_values == frozenset({"read", "write"})


def test_a_histogram_with_buckets_is_accepted() -> None:
    descriptor = MetricDescriptor(
        name="oktografx_fsync_duration_seconds",
        kind=MetricKind.HISTOGRAM,
        description="Time a durability barrier took, by target.",
        unit="seconds",
        labels=(LabelSpec(name="target", allowed_values=frozenset({"wal", "data"})),),
        buckets=(0.001, 0.005, 0.025, 0.1, 0.5, 2.5),
    )
    assert descriptor.buckets == (0.001, 0.005, 0.025, 0.1, 0.5, 2.5)
    assert descriptor.unit == "seconds"


@pytest.mark.parametrize("suffix", sorted(METRIC_UNIT_SUFFIXES))
def test_every_declared_unit_suffix_is_accepted(suffix: str) -> None:
    descriptor = MetricDescriptor(
        name=f"oktografx_probe{suffix}",
        kind=MetricKind.GAUGE,
        description="A probe metric used to prove the suffix contract.",
    )
    assert descriptor.name.endswith(suffix)


# --- rejected names -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "write_conflicts_total",
        "grafx_write_conflicts_total",
        "oktografxwrite_total",
    ],
)
def test_a_name_without_the_prefix_is_rejected(name: str) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        MetricDescriptor(name=name, kind=MetricKind.COUNTER, description="A metric.")
    assert raised.value.details["field"] == "name"


@pytest.mark.parametrize(
    "name",
    [
        "oktografx_Write_Conflicts_total",
        "oktografx__write_total",
        "oktografx_write_total_",
        "oktografx write total",
        "oktografx-write-total",
        "",
    ],
)
def test_a_name_that_is_not_snake_case_is_rejected(name: str) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        MetricDescriptor(name=name, kind=MetricKind.COUNTER, description="A metric.")
    assert raised.value.details["field"] == "name"


def test_a_non_string_name_is_rejected() -> None:
    with pytest.raises(GrafxConfigurationError):
        MetricDescriptor(name=7, kind=MetricKind.COUNTER, description="A metric.")  # type: ignore[arg-type]


@pytest.mark.parametrize("name", ["oktografx_write_conflicts", "oktografx_lease_wait_millis"])
def test_a_name_without_a_unit_suffix_is_rejected(name: str) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        MetricDescriptor(name=name, kind=MetricKind.COUNTER, description="A metric.")
    assert "unit suffixes" in raised.value.message


# --- rejected kind, description and unit ----------------------------------------------------


def test_a_kind_that_is_not_a_metric_kind_is_rejected() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        MetricDescriptor(
            name="oktografx_write_conflicts_total",
            kind="counter",  # type: ignore[arg-type]
            description="A metric.",
        )
    assert raised.value.details["field"] == "kind"


@pytest.mark.parametrize("description", ["", "   ", None, 7])
def test_an_empty_or_non_string_description_is_rejected(description: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        MetricDescriptor(
            name="oktografx_write_conflicts_total",
            kind=MetricKind.COUNTER,
            description=description,  # type: ignore[arg-type]
        )
    assert raised.value.details["field"] == "description"


def test_a_description_without_a_final_period_is_rejected() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        MetricDescriptor(
            name="oktografx_write_conflicts_total",
            kind=MetricKind.COUNTER,
            description="Commits refused by optimistic validation",
        )
    assert raised.value.details["field"] == "description"


@pytest.mark.parametrize("unit", ["Seconds", "per second", 3])
def test_an_invalid_unit_is_rejected(unit: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        MetricDescriptor(
            name="oktografx_write_conflicts_total",
            kind=MetricKind.COUNTER,
            description="A metric.",
            unit=unit,  # type: ignore[arg-type]
        )
    assert raised.value.details["field"] == "unit"


# --- rejected labels ------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(FORBIDDEN_LABEL_NAMES))
def test_every_forbidden_label_name_is_rejected(name: str) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        LabelSpec(name=name)
    assert "forbidden" in raised.value.message


@pytest.mark.parametrize("name", ["Mode", "read mode", "_mode", "mode_", "", 5])
def test_a_label_name_that_is_not_snake_case_is_rejected(name: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        LabelSpec(name=name)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "name"


def test_an_unenumerated_label_may_not_exceed_the_cardinality_limit() -> None:
    accepted = LabelSpec(name="db", max_cardinality=UNBOUNDED_LABEL_CARDINALITY_LIMIT)
    assert accepted.allowed_values is None
    with pytest.raises(GrafxConfigurationError) as raised:
        LabelSpec(name="db", max_cardinality=UNBOUNDED_LABEL_CARDINALITY_LIMIT + 1)
    assert raised.value.details["field"] == "max_cardinality"


def test_an_enumerated_label_may_declare_a_large_bound() -> None:
    label = LabelSpec(
        name="regime",
        allowed_values=frozenset({"exact", "approximate"}),
        max_cardinality=1024,
    )
    assert label.max_cardinality == 1024


@pytest.mark.parametrize("max_cardinality", [0, -1, "8", True])
def test_an_invalid_max_cardinality_is_rejected(max_cardinality: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        LabelSpec(name="mode", max_cardinality=max_cardinality)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "max_cardinality"


def test_an_empty_value_domain_is_rejected() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        LabelSpec(name="mode", allowed_values=frozenset())
    assert raised.value.details["field"] == "allowed_values"


def test_a_value_domain_that_is_not_a_frozenset_is_rejected() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        LabelSpec(name="mode", allowed_values={"read", "write"})  # type: ignore[arg-type]
    assert raised.value.details["field"] == "allowed_values"


def test_a_value_domain_with_a_non_string_is_rejected() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        LabelSpec(name="mode", allowed_values=frozenset({"read", 7}))  # type: ignore[arg-type]
    assert raised.value.details["field"] == "allowed_values"


def test_a_value_domain_larger_than_its_declared_bound_is_rejected() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        LabelSpec(
            name="mode",
            allowed_values=frozenset({"a", "b", "c"}),
            max_cardinality=2,
        )
    assert raised.value.details["field"] == "max_cardinality"


def test_labels_must_be_a_tuple_of_label_specs() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        MetricDescriptor(
            name="oktografx_write_conflicts_total",
            kind=MetricKind.COUNTER,
            description="A metric.",
            labels=[LabelSpec(name="mode")],  # type: ignore[arg-type]
        )
    assert raised.value.details["field"] == "labels"

    with pytest.raises(GrafxConfigurationError) as raised:
        MetricDescriptor(
            name="oktografx_write_conflicts_total",
            kind=MetricKind.COUNTER,
            description="A metric.",
            labels=("mode",),  # type: ignore[arg-type]
        )
    assert raised.value.details["field"] == "labels"


def test_a_repeated_label_name_is_rejected() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        MetricDescriptor(
            name="oktografx_write_conflicts_total",
            kind=MetricKind.COUNTER,
            description="A metric.",
            labels=(LabelSpec(name="mode"), LabelSpec(name="mode", max_cardinality=4)),
        )
    assert "more than once" in raised.value.message


# --- rejected buckets -----------------------------------------------------------------------


@pytest.mark.parametrize("kind", [MetricKind.COUNTER, MetricKind.GAUGE])
def test_buckets_on_a_non_histogram_are_rejected(kind: MetricKind) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        MetricDescriptor(
            name="oktografx_write_conflicts_total",
            kind=kind,
            description="A metric.",
            buckets=(0.1, 0.2),
        )
    assert raised.value.details["field"] == "buckets"


def test_a_histogram_without_buckets_is_rejected() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        MetricDescriptor(
            name="oktografx_fsync_duration_seconds",
            kind=MetricKind.HISTOGRAM,
            description="A metric.",
        )
    assert raised.value.details["field"] == "buckets"


def test_buckets_must_be_a_tuple() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        MetricDescriptor(
            name="oktografx_fsync_duration_seconds",
            kind=MetricKind.HISTOGRAM,
            description="A metric.",
            buckets=[0.1, 0.2],  # type: ignore[arg-type]
        )
    assert raised.value.details["field"] == "buckets"


@pytest.mark.parametrize(
    "buckets",
    [
        (0.2, 0.1),
        (0.1, 0.1),
        (0.1, float("inf")),
        (0.1, float("nan")),
        (0.1, "0.2"),
        (0.1, True),
    ],
)
def test_invalid_bucket_boundaries_are_rejected(buckets: tuple[object, ...]) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        MetricDescriptor(
            name="oktografx_fsync_duration_seconds",
            kind=MetricKind.HISTOGRAM,
            description="A metric.",
            buckets=buckets,  # type: ignore[arg-type]
        )
    assert raised.value.details["field"] == "buckets"


def test_descriptors_are_frozen_and_hashable() -> None:
    descriptor = MetricDescriptor(
        name="oktografx_wal_size_bytes",
        kind=MetricKind.GAUGE,
        description="Total size of the write-ahead log on disk.",
    )
    assert len({descriptor, descriptor}) == 1
    assert not hasattr(descriptor, "__dict__")


# --- the description must be en-US (G1, G7) ---------------------------------------------------


def _descriptor(description: str) -> MetricDescriptor:
    """Build a valid counter carrying the description under test."""
    return MetricDescriptor(
        name="oktografx_write_conflicts_total",
        kind=MetricKind.COUNTER,
        description=description,
    )


@pytest.mark.parametrize(
    "description",
    [
        "Commits refused by optimistic validation because partition sets intersect.",
        "Total size of the write-ahead log on disk.",
        "Time a durability barrier took, by target.",
        "Records discarded by recovery, by origin class.",
        "Pages resident in the buffer pool of this database.",
    ],
)
def test_an_en_us_description_is_accepted(description: str) -> None:
    assert _descriptor(description).description == description


@pytest.mark.parametrize(
    "description",
    [
        "Commits recusados pela validacao otimista.",
        "Registros descartados pela recuperacao.",
        "O total de bytes que o log ocupa, para o operador.",
        "Cada limpeza e uma entrada que deve aparecer no ledger.",
        "Quando o snapshot avanca, os segmentos sao reciclados.",
    ],
)
def test_a_pt_br_description_is_rejected_at_registration(description: str) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        _descriptor(description)
    assert raised.value.details["field"] == "description"
    assert "marker" in raised.value.message


def test_a_description_outside_ascii_is_rejected() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        _descriptor("Numero de commits recusados pela valida\u00e7\u00e3o.")
    assert raised.value.details["field"] == "description"


@pytest.mark.parametrize("description", [".", "..", "Conflicts.", "   Total.  "])
def test_a_description_that_says_nothing_is_rejected(description: str) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        _descriptor(description)
    assert raised.value.details["field"] == "description"


def test_the_registration_check_uses_the_shared_marker_list() -> None:
    assert NON_EN_US_MARKERS
    assert all(marker.isascii() and marker == marker.lower() for marker in NON_EN_US_MARKERS)
    for marker in NON_EN_US_MARKERS:
        with pytest.raises(GrafxConfigurationError):
            _descriptor(f"A description that carries{marker}marker.")
