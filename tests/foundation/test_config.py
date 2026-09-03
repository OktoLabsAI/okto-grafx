"""DatabaseConfig validation (CONTRACT.md section 5, SPEC-M1 FR-4 and TR-4)."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.page import validate_page_size
from okto_grafx.domain.query.limits import (
    DEFAULT_MAX_QUERY_VALUE_CHARACTERS,
    MAX_QUERY_VALUE_CHARACTERS,
)
from okto_grafx.engine.wal_manager import MAX_SEGMENT_READ_BYTES, MIN_SEGMENT_BYTES
from okto_grafx.runtime.config import (
    CORE_MAX_PAGE_SIZE,
    CORE_MIN_PAGE_SIZE,
    DEFAULT_OPENMETRICS_DESTINATION,
    DESCRIPTOR_REVALIDATION_MODES,
    MAX_PAGE_SIZE,
    MAX_PARTITIONS_PER_TABLE,
    MAX_VECTOR_EF_SEARCH,
    METRICS_SINKS,
    MIN_PAGE_SIZE,
    MINIMUM_STORE_FRAMES,
    RECOVERY_POLICIES,
    VECTOR_MATH_SELECTORS,
    DatabaseConfig,
)


def test_defaults_match_the_contract() -> None:
    config = DatabaseConfig(path="./mydb")
    assert config.path == "./mydb"
    assert config.page_size == 8192
    assert config.partitions_per_table == 64
    assert config.identity_lease_size == 64
    assert config.buffer_budget_bytes == 64 * 1024 * 1024
    assert config.max_open_files == 128
    assert config.recovery_policy == "replay"
    assert config.lease_ttl_seconds == 5.0
    assert config.lease_timeout_seconds == 10.0
    assert config.commit_lock_timeout_seconds == 30.0
    assert config.reader_stall_threshold_seconds == 15.0
    assert config.wal_segment_bytes == 4 * 1024 * 1024
    assert config.wal_max_bytes is None
    assert config.checkpoint_interval_records == 512
    assert config.max_statement_writes is None
    assert config.max_result_rows is None
    assert config.max_intermediate_rows is None
    assert config.query_memory_budget_bytes is None
    assert config.max_traversal_expansions is None
    assert config.max_traversal_paths is None
    assert config.max_query_value_characters == DEFAULT_MAX_QUERY_VALUE_CHARACTERS
    assert config.max_transaction_rows is None
    assert config.max_transaction_bytes is None
    assert config.max_wal_batch_bytes is None
    assert config.max_index_build_entries is None
    assert config.metrics == "noop"
    assert config.metrics_destination is None
    assert config.allow_remote_metrics is False
    assert config.vector_math == "auto"
    assert config.vector_exact_scan_threshold == 4096
    assert config.vector_ef_search == 320
    assert "vector_recall_target" not in {
        field.name for field in dataclasses.fields(DatabaseConfig)
    }
    assert config.read_only is False
    assert config.descriptor_revalidation == "strict"


def test_descriptor_revalidation_extends_the_positional_surface_only_at_its_tail() -> (
    None
):
    """An old positional call must still bind its last argument to ``read_only``."""
    configured = DatabaseConfig(path=":memory:", read_only=True)
    fields = tuple(
        field for field in dataclasses.fields(DatabaseConfig) if not field.kw_only
    )
    assert next(
        field
        for field in dataclasses.fields(DatabaseConfig)
        if field.name == "max_index_build_entries"
    ).kw_only
    assert tuple(field.name for field in fields[-3:]) == (
        "read_only",
        "descriptor_revalidation",
        "max_query_value_characters",
    )

    legacy_arguments = tuple(getattr(configured, field.name) for field in fields[:-2])
    legacy = DatabaseConfig(*legacy_arguments)
    assert legacy == configured
    assert legacy.read_only is True
    assert legacy.descriptor_revalidation == "strict"

    opted_in = DatabaseConfig(*(legacy_arguments + ("generation",)))
    assert opted_in.read_only is True
    assert opted_in.descriptor_revalidation == "generation"


def test_query_value_character_limit_is_bounded_and_extends_the_positional_tail() -> (
    None
):
    base = DatabaseConfig(path=":memory:")
    positional = tuple(
        getattr(base, field.name)
        for field in dataclasses.fields(DatabaseConfig)
        if not field.kw_only
    )
    configured = DatabaseConfig(
        ":memory:",
        *(positional[1:-2] + ("generation", 70_000)),
    )
    assert configured.descriptor_revalidation == "generation"
    assert configured.max_query_value_characters == 70_000

    for rejected in (0, -1, MAX_QUERY_VALUE_CHARACTERS + 1):
        with pytest.raises(GrafxConfigurationError) as caught:
            DatabaseConfig(path=":memory:", max_query_value_characters=rejected)
        assert caught.value.details["field"] == "max_query_value_characters"


def test_the_config_is_a_frozen_value() -> None:
    config = DatabaseConfig(path=":memory:")
    assert not hasattr(config, "__dict__")
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.page_size = 4096  # type: ignore[misc]


def test_granularity_descriptor_is_exactly_the_frozen_string() -> None:
    assert (
        DatabaseConfig(path=":memory:").granularity_descriptor
        == "hash-v1;partitions_per_table=64"
    )
    assert (
        DatabaseConfig(path=":memory:", partitions_per_table=256).granularity_descriptor
        == "hash-v1;partitions_per_table=256"
    )


def test_changing_the_partition_count_only_changes_the_descriptor() -> None:
    # SPEC-M1 FR-4: the record format does not change with the granularity parameter.
    first = DatabaseConfig(path=":memory:", partitions_per_table=64)
    second = dataclasses.replace(first, partitions_per_table=1024)
    assert first.granularity_descriptor != second.granularity_descriptor
    assert second.granularity_descriptor == "hash-v1;partitions_per_table=1024"


def test_the_memory_path_is_accepted() -> None:
    assert DatabaseConfig(path=":memory:").path == ":memory:"


# --- page size ------------------------------------------------------------------------------


@pytest.mark.parametrize("page_size", [512, 1024, 4096, 8192, 16384, 32768])
def test_a_power_of_two_page_size_in_range_is_accepted(page_size: int) -> None:
    assert DatabaseConfig(path=":memory:", page_size=page_size).page_size == page_size


@pytest.mark.parametrize(
    "page_size", [0, -8192, 4097, 3000, 256, 65536, 131072, "8192", 8192.0, True]
)
def test_an_invalid_page_size_is_rejected(page_size: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", page_size=page_size)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "page_size"
    assert "page_size" in raised.value.message


def test_the_page_size_bounds_are_the_declared_ones() -> None:
    assert (
        DatabaseConfig(path=":memory:", page_size=MIN_PAGE_SIZE).page_size
        == MIN_PAGE_SIZE
    )
    assert (
        DatabaseConfig(path=":memory:", page_size=MAX_PAGE_SIZE).page_size
        == MAX_PAGE_SIZE
    )
    with pytest.raises(GrafxConfigurationError):
        DatabaseConfig(path=":memory:", page_size=MIN_PAGE_SIZE // 2)
    with pytest.raises(GrafxConfigurationError):
        DatabaseConfig(path=":memory:", page_size=MAX_PAGE_SIZE * 2)


# --- partitions -----------------------------------------------------------------------------


@pytest.mark.parametrize("partitions", [1, 64, 1024, MAX_PARTITIONS_PER_TABLE])
def test_a_valid_partition_count_is_accepted(partitions: int) -> None:
    config = DatabaseConfig(path=":memory:", partitions_per_table=partitions)
    assert config.partitions_per_table == partitions


def test_the_partition_ceiling_is_the_number_the_format_can_store() -> None:
    # A68: a literal, not MAX + 1. The independent consequence is the format itself -- section
    # 6.2 stores partitions_per_table as u16, so 65535 is the largest value the meta page can
    # hold and 70000 would be accepted by a config the page cannot represent.
    assert MAX_PARTITIONS_PER_TABLE == 65535
    assert MAX_PARTITIONS_PER_TABLE == 2**16 - 1
    for unstorable in (65536, 70000, 2**17):
        with pytest.raises(GrafxConfigurationError):
            DatabaseConfig(path=":memory:", partitions_per_table=unstorable)


@pytest.mark.parametrize("partitions", [0, -1, 65536, 70000, "64", 64.0, None, True])
def test_an_invalid_partition_count_is_rejected(partitions: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", partitions_per_table=partitions)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "partitions_per_table"


# --- budgets and intervals ------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "identity_lease_size",
        "buffer_budget_bytes",
        "max_open_files",
        "wal_segment_bytes",
        "checkpoint_interval_records",
    ],
)
@pytest.mark.parametrize("value", [0, -1, "1024", 1024.5, None, True])
def test_a_non_positive_integer_budget_is_rejected(field: str, value: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", **{field: value})
    assert raised.value.details["field"] == field


def test_a_positive_open_file_budget_is_canonicalized() -> None:
    config = DatabaseConfig(path=":memory:", max_open_files=_HostileInt(73))

    assert type(config.max_open_files) is int
    assert config.max_open_files == 73


@pytest.mark.parametrize("value", [0, -1, "1024", 1024.5, True])
def test_an_invalid_wal_maximum_is_rejected(value: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", wal_max_bytes=value)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "wal_max_bytes"


def test_a_positive_wal_maximum_is_canonicalized() -> None:
    config = DatabaseConfig(path=":memory:", wal_max_bytes=_HostileInt(8192))
    assert type(config.wal_max_bytes) is int
    assert config.wal_max_bytes == 8192


@pytest.mark.parametrize(
    "field",
    [
        "max_statement_writes",
        "max_transaction_rows",
        "max_transaction_bytes",
        "max_wal_batch_bytes",
        "max_index_build_entries",
    ],
)
@pytest.mark.parametrize("value", [0, -1, "1024", 1024.5, True])
def test_an_invalid_transaction_budget_is_rejected(field: str, value: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", **{field: value})
    assert raised.value.details["field"] == field


@pytest.mark.parametrize(
    "field",
    [
        "max_result_rows",
        "max_intermediate_rows",
        "query_memory_budget_bytes",
        "max_traversal_expansions",
        "max_traversal_paths",
    ],
)
@pytest.mark.parametrize("value", [0, -1, "1024", 1024.5, True])
def test_an_invalid_query_budget_is_rejected(field: str, value: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", **{field: value})
    assert raised.value.details["field"] == field


def test_the_buffer_budget_must_hold_both_store_working_sets() -> None:
    page_size = 4096
    minimum = MINIMUM_STORE_FRAMES * page_size
    assert (
        DatabaseConfig(
            path=":memory:", page_size=page_size, buffer_budget_bytes=minimum
        ).buffer_budget_bytes
        == minimum
    )
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(
            path=":memory:", page_size=page_size, buffer_budget_bytes=minimum - 1
        )
    assert raised.value.details["field"] == "buffer_budget_bytes"


def test_the_wal_segment_bounds_are_exactly_the_reader_bounds() -> None:
    assert MIN_SEGMENT_BYTES == 256
    assert MAX_SEGMENT_READ_BYTES == 1024 * 1024 * 1024
    for accepted in (MIN_SEGMENT_BYTES, MAX_SEGMENT_READ_BYTES):
        assert (
            DatabaseConfig(
                path=":memory:", wal_segment_bytes=accepted
            ).wal_segment_bytes
            == accepted
        )
    for rejected in (MIN_SEGMENT_BYTES - 1, MAX_SEGMENT_READ_BYTES + 1):
        with pytest.raises(GrafxConfigurationError) as raised:
            DatabaseConfig(path=":memory:", wal_segment_bytes=rejected)
        assert raised.value.details["field"] == "wal_segment_bytes"


@pytest.mark.parametrize(
    "field",
    [
        "lease_ttl_seconds",
        "lease_timeout_seconds",
        "commit_lock_timeout_seconds",
        "reader_stall_threshold_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1.0, "5", None, float("nan"), float("inf")])
def test_a_non_positive_timeout_is_rejected(field: str, value: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", **{field: value})
    assert raised.value.details["field"] == field


@pytest.mark.parametrize(
    "field",
    [
        "lease_ttl_seconds",
        "lease_timeout_seconds",
        "commit_lock_timeout_seconds",
        "reader_stall_threshold_seconds",
    ],
)
def test_an_integer_timeout_is_accepted(field: str) -> None:
    config = DatabaseConfig(path=":memory:", **{field: 3})
    assert getattr(config, field) == 3


# --- path -----------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["", None, 7, b"./mydb"])
def test_an_invalid_path_is_rejected(path: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=path)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "path"


# --- enumerated choices ---------------------------------------------------------------------


@pytest.mark.parametrize("policy", sorted(RECOVERY_POLICIES))
def test_every_recovery_policy_is_accepted(policy: str) -> None:
    assert (
        DatabaseConfig(path=":memory:", recovery_policy=policy).recovery_policy
        == policy
    )


@pytest.mark.parametrize("mode", sorted(DESCRIPTOR_REVALIDATION_MODES))
def test_every_descriptor_revalidation_mode_is_accepted(mode: str) -> None:
    assert (
        DatabaseConfig(
            path=":memory:", descriptor_revalidation=mode
        ).descriptor_revalidation
        == mode
    )


@pytest.mark.parametrize("sink", sorted(METRICS_SINKS))
def test_every_metrics_sink_is_accepted(sink: str) -> None:
    # The JSON sink writes to a file, so it is the one sink that needs a destination (A8).
    destination = "./metrics.json" if sink == "json" else None
    config = DatabaseConfig(
        path=":memory:", metrics=sink, metrics_destination=destination
    )
    assert config.metrics == sink
    assert config.metrics_destination == destination


@pytest.mark.parametrize("selector", sorted(VECTOR_MATH_SELECTORS))
def test_every_vector_math_selector_is_accepted(selector: str) -> None:
    assert DatabaseConfig(path=":memory:", vector_math=selector).vector_math == selector


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("recovery_policy", "truncate"),
        ("recovery_policy", "REPLAY"),
        ("recovery_policy", None),
        ("recovery_policy", ["replay"]),
        ("metrics", "prometheus"),
        ("metrics", ""),
        ("metrics", 1),
        ("vector_math", "torch"),
        ("vector_math", None),
        ("descriptor_revalidation", "always"),
        ("descriptor_revalidation", "GENERATION"),
        ("descriptor_revalidation", None),
        ("descriptor_revalidation", 1),
    ],
)
def test_a_value_outside_an_enumerated_choice_is_rejected(
    field: str, value: object
) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", **{field: value})
    assert raised.value.details["field"] == field


# --- vector settings ------------------------------------------------------------------------


@pytest.mark.parametrize("threshold", [0, 1, 4096, 1_000_000])
def test_a_non_negative_exact_scan_threshold_is_accepted(threshold: int) -> None:
    config = DatabaseConfig(path=":memory:", vector_exact_scan_threshold=threshold)
    assert config.vector_exact_scan_threshold == threshold


@pytest.mark.parametrize("threshold", [-1, "4096", 4096.0, None])
def test_an_invalid_exact_scan_threshold_is_rejected(threshold: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", vector_exact_scan_threshold=threshold)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "vector_exact_scan_threshold"


@pytest.mark.parametrize("width", [1, 320, 4096, MAX_VECTOR_EF_SEARCH])
def test_a_bounded_hnsw_search_beam_is_accepted(width: int) -> None:
    config = DatabaseConfig(path=":memory:", vector_ef_search=width)
    assert config.vector_ef_search == width
    assert type(config.vector_ef_search) is int


@pytest.mark.parametrize(
    "width", [0, -1, MAX_VECTOR_EF_SEARCH + 1, "320", 320.0, None, True]
)
def test_an_invalid_hnsw_search_beam_is_rejected(width: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", vector_ef_search=width)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "vector_ef_search"


# --- read only ------------------------------------------------------------------------------


@pytest.mark.parametrize("read_only", [0, 1, "true", None])
def test_a_non_boolean_read_only_flag_is_rejected(read_only: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", read_only=read_only)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "read_only"


def test_read_only_accepts_both_boolean_values() -> None:
    assert DatabaseConfig(path=":memory:", read_only=True).read_only is True
    assert DatabaseConfig(path=":memory:", read_only=False).read_only is False


# --- error surface --------------------------------------------------------------------------


def test_the_rejection_message_names_the_field_and_the_value() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", page_size=3000)
    error = raised.value
    assert error.code == "configuration_error"
    assert error.retryable is False
    assert "page_size" in error.message
    assert "3000" in error.message
    assert error.details == {"field": "page_size", "value": 3000}


# --- metrics destination (amendment A8) -------------------------------------------------------


def test_the_json_sink_requires_a_destination() -> None:
    config = DatabaseConfig(
        path=":memory:", metrics="json", metrics_destination="./m.json"
    )
    assert config.metrics_destination == "./m.json"
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", metrics="json")
    assert raised.value.details["field"] == "metrics_destination"
    assert "path" in raised.value.message


@pytest.mark.parametrize("destination", ["", "   "])
def test_the_json_sink_refuses_an_empty_destination(destination: str) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", metrics="json", metrics_destination=destination)
    assert raised.value.details["field"] == "metrics_destination"


def test_the_openmetrics_publisher_defaults_to_an_ephemeral_local_port() -> None:
    config = DatabaseConfig(path=":memory:", metrics="openmetrics")
    assert config.metrics_destination is None
    assert DEFAULT_OPENMETRICS_DESTINATION == "127.0.0.1:0"


@pytest.mark.parametrize(
    "destination", ["127.0.0.1:9100", "127.255.255.254:0", "[::1]:9100"]
)
def test_the_openmetrics_publisher_accepts_a_loopback_host_and_port(
    destination: str,
) -> None:
    config = DatabaseConfig(
        path=":memory:", metrics="openmetrics", metrics_destination=destination
    )
    assert config.metrics_destination == destination


@pytest.mark.parametrize(
    "destination",
    ["0.0.0.0:0", "192.0.2.1:9100", "localhost:9100", "example.com:9100", "[::]:0"],
)
def test_openmetrics_refuses_a_remote_bind_without_explicit_consent(
    destination: str,
) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(
            path=":memory:", metrics="openmetrics", metrics_destination=destination
        )
    assert raised.value.details["field"] == "metrics_destination"


@pytest.mark.parametrize(
    "destination", ["0.0.0.0:0", "localhost:9100", "example.com:9100", "[::]:0"]
)
def test_openmetrics_accepts_a_remote_bind_with_explicit_consent(
    destination: str,
) -> None:
    config = DatabaseConfig(
        path=":memory:",
        metrics="openmetrics",
        metrics_destination=destination,
        allow_remote_metrics=True,
    )
    assert config.metrics_destination == destination
    assert config.allow_remote_metrics is True


@pytest.mark.parametrize("value", [0, 1, "true", None])
def test_allow_remote_metrics_requires_a_boolean(value: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", allow_remote_metrics=value)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "allow_remote_metrics"


@pytest.mark.parametrize(
    ("metrics", "destination"), [("noop", None), ("json", "./metrics.json")]
)
def test_allow_remote_metrics_is_never_an_inert_option(
    metrics: str, destination: str | None
) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(
            path=":memory:",
            metrics=metrics,
            metrics_destination=destination,
            allow_remote_metrics=True,
        )
    assert raised.value.details["field"] == "allow_remote_metrics"


@pytest.mark.parametrize(
    "destination",
    [
        "9100",
        "localhost",
        "localhost:",
        ":9100",
        "host:abc",
        "host:²",
        "host:65536",
        "host:-1",
        "host:" + "9" * 5000,
        "::1:9100",
        "   ",
    ],
)
def test_the_openmetrics_publisher_refuses_a_malformed_destination(
    destination: str,
) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(
            path=":memory:", metrics="openmetrics", metrics_destination=destination
        )
    assert raised.value.details["field"] == "metrics_destination"


def test_the_noop_sink_refuses_any_destination() -> None:
    assert DatabaseConfig(path=":memory:").metrics_destination is None
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", metrics_destination="./m.json")
    assert raised.value.details["field"] == "metrics_destination"
    assert "no-op" in raised.value.message


@pytest.mark.parametrize("destination", [7, 0, b"./m.json", ["./m.json"], True])
def test_a_destination_that_is_not_a_string_is_refused(destination: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", metrics="json", metrics_destination=destination)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "metrics_destination"


# --- the page size the config accepts is a page the storage core can address (A20) ------------

CANDIDATE_PAGE_SIZES: tuple[int, ...] = (
    tuple(2**exponent for exponent in range(0, 21))
    + tuple(range(500, 601))
    + tuple(range(32700, 32801))
    + (0, -8192, 3000, 4097, 12288)
)
"""Every power of two up to a million, plus dense ranges around both bounds and a few odd sizes.

The dense ranges matter: an off-by-one in either definition shows up as one size accepted on one
side of the boundary and refused on the other, which a sparse list of powers of two would miss.
"""


def _config_accepts(page_size: int) -> bool:
    try:
        DatabaseConfig(path=":memory:", page_size=page_size)
    except GrafxConfigurationError:
        return False
    return True


def _storage_core_accepts(page_size: int) -> bool:
    try:
        validate_page_size(page_size)
    except GrafxConfigurationError:
        return False
    return True


def test_both_page_size_bounds_are_the_storage_core_definitions() -> None:
    # Amendment A24: one definition, owned by C1. Both bounds are the imported objects, not
    # literals that happen to agree, so nobody can edit one of a pair into a divergence.
    assert MIN_PAGE_SIZE is CORE_MIN_PAGE_SIZE
    assert MAX_PAGE_SIZE is CORE_MAX_PAGE_SIZE
    assert (MIN_PAGE_SIZE, MAX_PAGE_SIZE) == (512, 32768)


def test_the_config_and_the_storage_core_accept_exactly_the_same_page_sizes() -> None:
    # Amendments A20 and A24: one accepted set, checked from both sides. A size accepted here
    # and refused on the first page write is the integration failure; a size accepted by the
    # core and refused here is the drift that shows the two definitions have parted company.
    accepted_here = {size for size in CANDIDATE_PAGE_SIZES if _config_accepts(size)}
    accepted_by_core = {
        size for size in CANDIDATE_PAGE_SIZES if _storage_core_accepts(size)
    }
    assert accepted_here - accepted_by_core == set(), (
        "the config accepts a page the core refuses"
    )
    assert accepted_by_core - accepted_here == set(), (
        "the core accepts a page the config refuses"
    )
    assert accepted_here == {512, 1024, 2048, 4096, 8192, 16384, 32768}


def test_the_cross_check_really_exercises_both_boundaries() -> None:
    # Guards the comparison above against passing on a candidate list that never touches an edge.
    assert 512 in CANDIDATE_PAGE_SIZES and 511 in CANDIDATE_PAGE_SIZES
    assert 32768 in CANDIDATE_PAGE_SIZES and 32769 in CANDIDATE_PAGE_SIZES
    assert 256 in CANDIDATE_PAGE_SIZES and 65536 in CANDIDATE_PAGE_SIZES
    assert len(CANDIDATE_PAGE_SIZES) >= 200


def test_the_configuration_defers_to_the_storage_core_validator() -> None:
    # A34: the local range check masks this call behaviourally, so deleting it survives every
    # behavioural test -- and it is the single line that would catch C0 and C1 drifting apart,
    # which is its entire purpose under A20/A24. Pinned structurally, for want of a behavioural
    # signal that could exist while the two agree.
    config_module = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "okto_grafx"
        / "runtime"
        / "config.py"
    )
    source = config_module.read_text(encoding="utf-8")
    body = source[
        source.index("def __post_init__") : source.index("def _validate_metrics")
    ]
    assert "validate_page_size(page_size)" in body, (
        "__post_init__ must put the surviving size to the storage core, so a disagreement "
        "between the two bounds is impossible rather than merely untested"
    )


def test_the_default_page_size_is_valid_for_both() -> None:
    default = DatabaseConfig(path=":memory:").page_size
    assert _config_accepts(default)
    assert _storage_core_accepts(default)


def test_the_page_that_cannot_address_its_own_tail_is_refused() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", page_size=65536)
    assert raised.value.details["field"] == "page_size"
    assert "65536" in raised.value.message
    assert "page_size" in raised.value.message


# --- hostile scalar subclasses ---------------------------------------------------------------


class _HostileInt(int):
    """An integer-shaped caller value whose Python-level hooks must never run."""

    def __int__(self) -> int:
        raise RuntimeError("caller __int__ ran")

    def __index__(self) -> int:
        raise RuntimeError("caller __index__ ran")

    def __format__(self, specification: str) -> str:
        raise RuntimeError("caller __format__ ran")

    def __repr__(self) -> str:
        raise RuntimeError("caller __repr__ ran")


class _HostileFloat(float):
    """A float subclass that carries callbacks instead of being a plain configuration leaf."""

    def __float__(self) -> float:
        raise RuntimeError("caller __float__ ran")

    def __repr__(self) -> str:
        raise RuntimeError("caller __repr__ ran")


class _HostileStr(str):
    """A string subclass whose comparison, formatting and text methods are capabilities."""

    def __str__(self) -> str:
        raise RuntimeError("caller __str__ ran")

    def __repr__(self) -> str:
        raise RuntimeError("caller __repr__ ran")

    def __format__(self, specification: str) -> str:
        raise RuntimeError("caller __format__ ran")

    def __hash__(self) -> int:
        raise RuntimeError("caller __hash__ ran")

    def __eq__(self, other: object) -> bool:
        raise RuntimeError("caller __eq__ ran")

    def strip(self, characters: str | None = None) -> str:
        raise RuntimeError("caller strip ran")


def test_configuration_canonicalizes_every_integer_leaf_before_using_it() -> None:
    config = DatabaseConfig(
        path=":memory:",
        page_size=_HostileInt(8192),
        partitions_per_table=_HostileInt(256),
        identity_lease_size=_HostileInt(32),
        buffer_budget_bytes=_HostileInt(8192 * 8),
        wal_segment_bytes=_HostileInt(4096),
        wal_max_bytes=_HostileInt(8192),
        checkpoint_interval_records=_HostileInt(32),
        max_statement_writes=_HostileInt(64),
        max_result_rows=_HostileInt(256),
        max_intermediate_rows=_HostileInt(512),
        query_memory_budget_bytes=_HostileInt(4096),
        max_traversal_expansions=_HostileInt(1024),
        max_traversal_paths=_HostileInt(2048),
        max_query_value_characters=_HostileInt(65_536),
        max_transaction_rows=_HostileInt(128),
        max_transaction_bytes=_HostileInt(16384),
        max_wal_batch_bytes=_HostileInt(8192),
        max_index_build_entries=_HostileInt(4096),
        vector_exact_scan_threshold=_HostileInt(128),
        vector_ef_search=_HostileInt(640),
    )

    for field in (
        "page_size",
        "partitions_per_table",
        "identity_lease_size",
        "buffer_budget_bytes",
        "wal_segment_bytes",
        "wal_max_bytes",
        "checkpoint_interval_records",
        "max_statement_writes",
        "max_result_rows",
        "max_intermediate_rows",
        "query_memory_budget_bytes",
        "max_traversal_expansions",
        "max_traversal_paths",
        "max_query_value_characters",
        "max_transaction_rows",
        "max_transaction_bytes",
        "max_wal_batch_bytes",
        "max_index_build_entries",
        "vector_exact_scan_threshold",
        "vector_ef_search",
    ):
        assert type(getattr(config, field)) is int
    assert config.granularity_descriptor == "hash-v1;partitions_per_table=256"


def test_configuration_canonicalizes_every_real_leaf_before_using_it() -> None:
    config = DatabaseConfig(
        path=":memory:",
        lease_ttl_seconds=_HostileFloat(1.0),
        lease_timeout_seconds=_HostileInt(2),
        commit_lock_timeout_seconds=_HostileFloat(3.0),
        reader_stall_threshold_seconds=_HostileInt(4),
    )

    for field in (
        "lease_ttl_seconds",
        "lease_timeout_seconds",
        "commit_lock_timeout_seconds",
        "reader_stall_threshold_seconds",
    ):
        assert type(getattr(config, field)) is float


def test_configuration_canonicalizes_every_text_leaf_before_using_it() -> None:
    config = DatabaseConfig(
        path=_HostileStr("./db"),
        recovery_policy=_HostileStr("replay"),
        metrics=_HostileStr("json"),
        metrics_destination=_HostileStr("./metrics.json"),
        vector_math=_HostileStr("pure"),
        checksum=_HostileStr("pure"),
        descriptor_revalidation=_HostileStr("generation"),
    )

    for field in (
        "path",
        "recovery_policy",
        "metrics",
        "metrics_destination",
        "vector_math",
        "checksum",
        "descriptor_revalidation",
    ):
        assert type(getattr(config, field)) is str


class _HostileObject:
    def __repr__(self) -> str:
        raise RuntimeError("caller __repr__ ran")


def test_configuration_refusal_never_formats_a_hostile_caller_value() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", page_size=_HostileObject())  # type: ignore[arg-type]

    assert raised.value.details == {"field": "page_size", "value": "_HostileObject"}


@pytest.mark.parametrize(
    "field",
    [
        "lease_ttl_seconds",
        "lease_timeout_seconds",
        "commit_lock_timeout_seconds",
        "reader_stall_threshold_seconds",
    ],
)
def test_a_real_too_large_for_float_is_a_typed_configuration_refusal(
    field: str,
) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", **{field: 10**10_000})

    assert raised.value.details["field"] == field
    assert "finite" in raised.value.message
