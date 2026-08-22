"""Database configuration (CONTRACT.md section 5).

Every value is validated the moment the configuration object is built, in en-US, naming the
field that is wrong. A database never starts on a value it would only reject halfway through
the first transaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.page import MAX_PAGE_SIZE as CORE_MAX_PAGE_SIZE
from okto_grafx.domain.page import MIN_PAGE_SIZE as CORE_MIN_PAGE_SIZE
from okto_grafx.domain.page import validate_page_size

__all__ = [
    "MIN_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "CORE_MIN_PAGE_SIZE",
    "CORE_MAX_PAGE_SIZE",
    "MAX_PARTITIONS_PER_TABLE",
    "RECOVERY_POLICIES",
    "METRICS_SINKS",
    "VECTOR_MATH_SELECTORS",
    "MEMORY_PATH",
    "DEFAULT_OPENMETRICS_DESTINATION",
    "DatabaseConfig",
]

MIN_PAGE_SIZE: int = CORE_MIN_PAGE_SIZE
"""Smallest page a database may be configured with, owned by the storage core (A20, A24).

Derived, never repeated: a page below this size cannot hold its 32-byte header and a useful
payload, and the component that has to lay the page out is the one that knows it.
"""

MAX_PAGE_SIZE: int = CORE_MAX_PAGE_SIZE
"""Largest page a database may be configured with, owned by the storage core (A20, A24).

Derived for the same reason as the floor: ``free_start`` and ``free_end`` are 16-bit fields, so
a 65536-byte page could not address its own tail. Two same-named constants that agree today are
a divergence waiting for someone to edit one of them, which is why both bounds and the validator
have exactly one definition, in :mod:`okto_grafx.domain.page`.
"""

MAX_PARTITIONS_PER_TABLE: int = 65535
"""Largest partition count: the meta page stores it as an unsigned 16-bit field."""

RECOVERY_POLICIES: frozenset[str] = frozenset({"replay", "refuse"})
"""Replay up to the last intact record (default), or refuse to open a damaged database."""

METRICS_SINKS: frozenset[str] = frozenset({"noop", "openmetrics", "json"})
"""The metrics adapters the composition root knows how to build."""

VECTOR_MATH_SELECTORS: frozenset[str] = frozenset({"auto", "pure", "numpy"})
"""Which vector math adapter to bind: detect, force the pure oracle, or force the accelerator."""

CHECKSUM_SELECTORS: frozenset[str] = frozenset({"auto", "pure", "native"})
"""Which CRC-32C implementation to install: detect, force the reference, or force the accelerator.

``"auto"`` accelerates when a provider is installed, and that is the OPPOSITE of what
``vector_math`` does with the same word. The difference is not a matter of taste: two vector math
adapters agree only to a stated tolerance, so binding whichever happened to be present would make
the ranking of a query depend on the machine. A checksum has no tolerance -- ``install_crc32c``
replays the acceptance corpus against ``crc32c_reference`` and REFUSES a candidate that disagrees
on any input, before it is installed -- so the bytes on disk are the same whichever is running,
and the only thing the selector changes is how long they take to compute.
"""


MEMORY_PATH: str = ":memory:"
"""The path that selects the in-memory storage device with identical transactional semantics."""

DEFAULT_OPENMETRICS_DESTINATION: str = "127.0.0.1:0"
"""Where the OpenMetrics publisher listens when the configuration names no destination.

Port zero means the operating system picks a free port, so a database never fails to open
because a fixed port was taken (amendment A8).
"""


def _reject(field: str, value: object, reason: str) -> GrafxConfigurationError:
    """Build the configuration error for one field, naming the field, the value and the reason."""
    return GrafxConfigurationError(
        f"Invalid configuration for {field!r}: {reason} Got {value!r}.",
        field=field,
        value=value,
    )


def _require_int(field: str, value: object) -> int:
    """Return the value as an int, rejecting booleans and anything that is not an integer."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise _reject(field, value, "an integer is required.")
    return value


def _require_positive_int(field: str, value: object) -> int:
    """Return the value as a strictly positive int."""
    number = _require_int(field, value)
    if number <= 0:
        raise _reject(field, value, "a value greater than zero is required.")
    return number


def _require_positive_number(field: str, value: object) -> float:
    """Return the value as a finite, strictly positive number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _reject(field, value, "a number is required.")
    number = float(value)
    if not isfinite(number):
        raise _reject(field, value, "a finite number is required.")
    if number <= 0.0:
        raise _reject(field, value, "a value greater than zero is required.")
    return number


def _require_choice(field: str, value: object, choices: frozenset[str]) -> str:
    """Return the value as one of the allowed string choices."""
    if not isinstance(value, str) or value not in choices:
        raise _reject(field, value, f"one of {', '.join(sorted(choices))} is required.")
    return value


@dataclass(frozen=True, slots=True)
class DatabaseConfig:
    """Everything the composition root needs to open one database, validated on construction."""

    path: str
    page_size: int = 8192
    partitions_per_table: int = 64
    buffer_budget_bytes: int = 64 * 1024 * 1024
    recovery_policy: str = "replay"
    lease_ttl_seconds: float = 5.0
    lease_timeout_seconds: float = 10.0
    commit_lock_timeout_seconds: float = 30.0
    reader_stall_threshold_seconds: float = 15.0
    wal_segment_bytes: int = 4 * 1024 * 1024
    checkpoint_interval_records: int = 512
    metrics: str = "noop"
    metrics_destination: str | None = None
    vector_math: str = "auto"
    checksum: str = "auto"
    vector_exact_scan_threshold: int = 4096
    vector_recall_target: float = 0.90
    read_only: bool = False

    def __post_init__(self) -> None:
        """Reject any unusable field with a GrafxConfigurationError that names it."""
        if not isinstance(self.path, str) or not self.path:
            raise _reject("path", self.path, "a non-empty string is required.")

        page_size = _require_positive_int("page_size", self.page_size)
        if page_size & (page_size - 1) != 0:
            raise _reject("page_size", self.page_size, "a power of two is required.")
        if not (MIN_PAGE_SIZE <= page_size <= MAX_PAGE_SIZE):
            raise _reject(
                "page_size",
                self.page_size,
                f"a value between {MIN_PAGE_SIZE} and {MAX_PAGE_SIZE} is required.",
            )
        # The storage core has the final word on what it can address, so a size that survives the
        # configuration is put to it directly. This can only raise if the two ever disagree,
        # which is the drift amendment A20 exists to prevent.
        validate_page_size(page_size)

        partitions = _require_positive_int("partitions_per_table", self.partitions_per_table)
        if partitions > MAX_PARTITIONS_PER_TABLE:
            raise _reject(
                "partitions_per_table",
                self.partitions_per_table,
                f"a value of at most {MAX_PARTITIONS_PER_TABLE} is required.",
            )

        _require_positive_int("buffer_budget_bytes", self.buffer_budget_bytes)
        _require_positive_int("wal_segment_bytes", self.wal_segment_bytes)
        _require_positive_int("checkpoint_interval_records", self.checkpoint_interval_records)

        threshold = _require_int("vector_exact_scan_threshold", self.vector_exact_scan_threshold)
        if threshold < 0:
            raise _reject(
                "vector_exact_scan_threshold",
                self.vector_exact_scan_threshold,
                "a value of zero or more is required.",
            )

        _require_positive_number("lease_ttl_seconds", self.lease_ttl_seconds)
        _require_positive_number("lease_timeout_seconds", self.lease_timeout_seconds)
        _require_positive_number("commit_lock_timeout_seconds", self.commit_lock_timeout_seconds)
        _require_positive_number(
            "reader_stall_threshold_seconds", self.reader_stall_threshold_seconds
        )

        recall_target = _require_positive_number("vector_recall_target", self.vector_recall_target)
        if recall_target > 1.0:
            raise _reject(
                "vector_recall_target",
                self.vector_recall_target,
                "a value greater than zero and at most one is required.",
            )

        _require_choice("recovery_policy", self.recovery_policy, RECOVERY_POLICIES)
        _require_choice("metrics", self.metrics, METRICS_SINKS)
        _require_choice("vector_math", self.vector_math, VECTOR_MATH_SELECTORS)
        _require_choice("checksum", self.checksum, CHECKSUM_SELECTORS)
        self._validate_metrics_destination()

        if not isinstance(self.read_only, bool):
            raise _reject("read_only", self.read_only, "a boolean is required.")

    def _validate_metrics_destination(self) -> None:
        """Check the destination against the sink that will consume it (amendment A8).

        A JSON sink writes to a file and cannot invent a path; an OpenMetrics publisher listens
        on a host and port and has a sane default; the no-op sink has nowhere to send anything,
        so a destination there is a configuration mistake worth naming rather than ignoring.
        """
        destination = self.metrics_destination
        if destination is not None and not isinstance(destination, str):
            raise _reject("metrics_destination", destination, "a string or None is required.")

        if self.metrics == "noop":
            if destination is not None:
                raise _reject(
                    "metrics_destination",
                    destination,
                    "the no-op sink has no destination, so None is required.",
                )
            return

        if self.metrics == "json":
            if destination is None or not destination.strip():
                raise _reject(
                    "metrics_destination",
                    destination,
                    "the JSON sink writes to a file, so a non-empty path is required.",
                )
            return

        if destination is None:
            return
        if not destination.strip():
            raise _reject(
                "metrics_destination",
                destination,
                f"a non-empty host:port is required, or None for {DEFAULT_OPENMETRICS_DESTINATION}.",
            )
        host, separator, port = destination.rpartition(":")
        if not separator or not host or not port.isdigit() or not 0 <= int(port) <= 65535:
            raise _reject(
                "metrics_destination",
                destination,
                "the OpenMetrics publisher needs a host:port with a port between 0 and 65535.",
            )

    @property
    def granularity_descriptor(self) -> str:
        """Return the self-describing granularity string every transaction record carries (TR-4)."""
        return f"hash-v1;partitions_per_table={self.partitions_per_table}"
