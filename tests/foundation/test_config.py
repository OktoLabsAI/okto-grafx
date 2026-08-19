"""DatabaseConfig validation (CONTRACT.md section 5, SPEC-M1 FR-4 and TR-4)."""

from __future__ import annotations

import dataclasses

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.page import validate_page_size
from okto_grafx.runtime.config import (
    CORE_MAX_PAGE_SIZE,
    CORE_MIN_PAGE_SIZE,
    DEFAULT_OPENMETRICS_DESTINATION,
    MAX_PAGE_SIZE,
    MAX_PARTITIONS_PER_TABLE,
    METRICS_SINKS,
    MIN_PAGE_SIZE,
    RECOVERY_POLICIES,
    VECTOR_MATH_SELECTORS,
    DatabaseConfig,
)


def test_defaults_match_the_contract() -> None:
    config = DatabaseConfig(path="./mydb")
    assert config.path == "./mydb"
    assert config.page_size == 8192
    assert config.partitions_per_table == 64
    assert config.buffer_budget_bytes == 64 * 1024 * 1024
    assert config.recovery_policy == "replay"
    assert config.lease_ttl_seconds == 5.0
    assert config.lease_timeout_seconds == 10.0
    assert config.commit_lock_timeout_seconds == 30.0
    assert config.reader_stall_threshold_seconds == 15.0
    assert config.wal_segment_bytes == 4 * 1024 * 1024
    assert config.checkpoint_interval_records == 512
    assert config.metrics == "noop"
    assert config.metrics_destination is None
    assert config.vector_math == "auto"
    assert config.vector_exact_scan_threshold == 4096
    assert config.vector_recall_target == 0.90
    assert config.read_only is False


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
    assert DatabaseConfig(path=":memory:", page_size=MIN_PAGE_SIZE).page_size == MIN_PAGE_SIZE
    assert DatabaseConfig(path=":memory:", page_size=MAX_PAGE_SIZE).page_size == MAX_PAGE_SIZE
    with pytest.raises(GrafxConfigurationError):
        DatabaseConfig(path=":memory:", page_size=MIN_PAGE_SIZE // 2)
    with pytest.raises(GrafxConfigurationError):
        DatabaseConfig(path=":memory:", page_size=MAX_PAGE_SIZE * 2)


# --- partitions -----------------------------------------------------------------------------


@pytest.mark.parametrize("partitions", [1, 64, 1024, MAX_PARTITIONS_PER_TABLE])
def test_a_valid_partition_count_is_accepted(partitions: int) -> None:
    config = DatabaseConfig(path=":memory:", partitions_per_table=partitions)
    assert config.partitions_per_table == partitions


@pytest.mark.parametrize(
    "partitions", [0, -1, MAX_PARTITIONS_PER_TABLE + 1, "64", 64.0, None, True]
)
def test_an_invalid_partition_count_is_rejected(partitions: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", partitions_per_table=partitions)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "partitions_per_table"


# --- budgets and intervals ------------------------------------------------------------------


@pytest.mark.parametrize(
    "field",
    ["buffer_budget_bytes", "wal_segment_bytes", "checkpoint_interval_records"],
)
@pytest.mark.parametrize("value", [0, -1, "1024", 1024.5, None])
def test_a_non_positive_integer_budget_is_rejected(field: str, value: object) -> None:
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
    assert DatabaseConfig(path=":memory:", recovery_policy=policy).recovery_policy == policy


@pytest.mark.parametrize("sink", sorted(METRICS_SINKS))
def test_every_metrics_sink_is_accepted(sink: str) -> None:
    # The JSON sink writes to a file, so it is the one sink that needs a destination (A8).
    destination = "./metrics.json" if sink == "json" else None
    config = DatabaseConfig(path=":memory:", metrics=sink, metrics_destination=destination)
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
    ],
)
def test_a_value_outside_an_enumerated_choice_is_rejected(field: str, value: object) -> None:
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


@pytest.mark.parametrize("target", [0.5, 0.9, 1.0])
def test_a_recall_target_in_the_open_unit_interval_is_accepted(target: float) -> None:
    config = DatabaseConfig(path=":memory:", vector_recall_target=target)
    assert config.vector_recall_target == target


@pytest.mark.parametrize("target", [0.0, -0.1, 1.0001, 2, "0.9", None, float("nan")])
def test_an_invalid_recall_target_is_rejected(target: object) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", vector_recall_target=target)  # type: ignore[arg-type]
    assert raised.value.details["field"] == "vector_recall_target"


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
    config = DatabaseConfig(path=":memory:", metrics="json", metrics_destination="./m.json")
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
    "destination", ["127.0.0.1:9100", "0.0.0.0:0", "localhost:65535", "[::1]:9100"]
)
def test_the_openmetrics_publisher_accepts_a_host_and_port(destination: str) -> None:
    config = DatabaseConfig(
        path=":memory:", metrics="openmetrics", metrics_destination=destination
    )
    assert config.metrics_destination == destination


@pytest.mark.parametrize(
    "destination", ["9100", "localhost", "localhost:", ":9100", "host:abc", "host:65536", "host:-1", "   "]
)
def test_the_openmetrics_publisher_refuses_a_malformed_destination(destination: str) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        DatabaseConfig(path=":memory:", metrics="openmetrics", metrics_destination=destination)
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
        DatabaseConfig(
            path=":memory:", metrics="json", metrics_destination=destination
        )  # type: ignore[arg-type]
    assert raised.value.details["field"] == "metrics_destination"


# --- the page size the config accepts is a page the storage core can address (A20) ------------

CANDIDATE_PAGE_SIZES: tuple[int, ...] = tuple(2**exponent for exponent in range(6, 21)) + (
    0,
    -8192,
    3000,
    4097,
    12288,
)


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


def test_the_upper_bound_is_the_storage_core_definition() -> None:
    # Imported, not repeated: free_start and free_end are 16-bit, so 65536 cannot be addressed.
    assert MAX_PAGE_SIZE == CORE_MAX_PAGE_SIZE == 32768
    assert MIN_PAGE_SIZE == 512
    assert MIN_PAGE_SIZE >= CORE_MIN_PAGE_SIZE


def test_the_config_never_accepts_a_page_size_the_storage_core_refuses() -> None:
    # The integration failure amendment A20 closes: a size accepted here and refused on the
    # first page write. The configuration may be stricter, never looser.
    accepted_here = {size for size in CANDIDATE_PAGE_SIZES if _config_accepts(size)}
    accepted_by_core = {size for size in CANDIDATE_PAGE_SIZES if _storage_core_accepts(size)}
    assert accepted_here <= accepted_by_core
    assert accepted_here == {512, 1024, 2048, 4096, 8192, 16384, 32768}
    # The only sizes the two disagree on are below the configuration floor, on purpose.
    assert all(size < MIN_PAGE_SIZE for size in accepted_by_core - accepted_here)


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
