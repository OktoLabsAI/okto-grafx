"""Database configuration (CONTRACT.md section 5).

Every value is validated the moment the configuration object is built, in en-US, naming the
field that is wrong. A database never starts on a value it would only reject halfway through
the first transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from math import isfinite

from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.page import MAX_PAGE_SIZE as CORE_MAX_PAGE_SIZE
from okto_grafx.domain.page import MIN_PAGE_SIZE as CORE_MIN_PAGE_SIZE
from okto_grafx.domain.page import validate_page_size
from okto_grafx.engine.catalog_store import MINIMUM_FRAMES as CATALOG_FRAMES
from okto_grafx.engine.heap_store import MINIMUM_FRAMES as HEAP_FRAMES
from okto_grafx.engine.wal_manager import MAX_SEGMENT_READ_BYTES, MIN_SEGMENT_BYTES

__all__ = [
    "MIN_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "CORE_MIN_PAGE_SIZE",
    "CORE_MAX_PAGE_SIZE",
    "MAX_PARTITIONS_PER_TABLE",
    "MINIMUM_STORE_FRAMES",
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

MINIMUM_STORE_FRAMES: int = max(CATALOG_FRAMES, HEAP_FRAMES)
"""Fewest pages the catalog and heap stores can operate over at once."""

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


def _builtin_type_name(value: object) -> str:
    """Name a value's type without invoking a hostile metaclass descriptor."""
    value_type = type(value)
    declared = type.__dict__["__name__"].__get__(value_type, type(value_type))
    return str.__str__(declared)


def _diagnostic_value(value: object) -> object:
    """Return a capability-free value suitable for a public error detail."""
    value_type = type(value)
    if value is None or value_type is bool:
        return value
    if issubclass(value_type, str):
        plain = str.__str__(value)
        return (
            plain if str.__len__(plain) <= 256 else f"str<{str.__len__(plain)} chars>"
        )
    if issubclass(value_type, int):
        plain = int.__int__(value)
        return (
            plain
            if int.bit_length(plain) <= 256
            else f"int<{int.bit_length(plain)} bits>"
        )
    if issubclass(value_type, float):
        return float.__float__(value)
    return _builtin_type_name(value)


def _reject(field: str, value: object, reason: str) -> GrafxConfigurationError:
    """Build a typed refusal without executing caller-controlled formatting hooks."""
    observed = _diagnostic_value(value)
    return GrafxConfigurationError(
        f"Invalid configuration for {field!r}: {reason} Got {observed!r}.",
        field=field,
        value=observed,
    )


def _require_text(field: str, value: object, *, empty: bool = True) -> str:
    """Copy a string into an exact built-in value without invoking subclass hooks."""
    if not issubclass(type(value), str):
        qualification = "a string" if empty else "a non-empty string"
        raise _reject(field, value, f"{qualification} is required.")
    plain = str.__str__(value)
    if not empty and str.__len__(plain) == 0:
        raise _reject(field, plain, "a non-empty string is required.")
    return plain


def _require_int(field: str, value: object) -> int:
    """Copy an integer into an exact built-in value without invoking subclass hooks."""
    if type(value) is bool or not issubclass(type(value), int):
        raise _reject(field, value, "an integer is required.")
    return int.__int__(value)


def _require_positive_int(field: str, value: object) -> int:
    """Return the value as a strictly positive int."""
    number = _require_int(field, value)
    if number <= 0:
        raise _reject(field, value, "a value greater than zero is required.")
    return number


def _require_positive_number(field: str, value: object) -> float:
    """Copy a finite positive real into an exact float without caller callbacks."""
    value_type = type(value)
    if value_type is bool or not issubclass(value_type, (int, float)):
        raise _reject(field, value, "a number is required.")
    try:
        if issubclass(value_type, float):
            number = float.__float__(value)
        else:
            number = float(int.__int__(value))
    except OverflowError:
        raise _reject(field, value, "a finite number is required.") from None
    if not isfinite(number):
        raise _reject(field, value, "a finite number is required.")
    if number <= 0.0:
        raise _reject(field, value, "a value greater than zero is required.")
    return number


def _require_choice(field: str, value: object, choices: frozenset[str]) -> str:
    """Return an exact built-in string when it names one of the allowed choices."""
    if not issubclass(type(value), str):
        raise _reject(field, value, f"one of {', '.join(sorted(choices))} is required.")
    plain = str.__str__(value)
    if plain not in choices:
        raise _reject(field, plain, f"one of {', '.join(sorted(choices))} is required.")
    return plain


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
        path = _require_text("path", self.path, empty=False)
        object.__setattr__(self, "path", path)

        page_size = _require_positive_int("page_size", self.page_size)
        object.__setattr__(self, "page_size", page_size)
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

        partitions = _require_positive_int(
            "partitions_per_table", self.partitions_per_table
        )
        object.__setattr__(self, "partitions_per_table", partitions)
        if partitions > MAX_PARTITIONS_PER_TABLE:
            raise _reject(
                "partitions_per_table",
                self.partitions_per_table,
                f"a value of at most {MAX_PARTITIONS_PER_TABLE} is required.",
            )

        for field in (
            "buffer_budget_bytes",
            "wal_segment_bytes",
            "checkpoint_interval_records",
        ):
            object.__setattr__(
                self, field, _require_positive_int(field, getattr(self, field))
            )
        minimum_budget = MINIMUM_STORE_FRAMES * page_size
        if self.buffer_budget_bytes < minimum_budget:
            raise GrafxConfigurationError(
                f"Invalid configuration for 'buffer_budget_bytes': at least "
                f"{MINIMUM_STORE_FRAMES} pages ({minimum_budget} bytes) are required. "
                f"Got {self.buffer_budget_bytes!r}.",
                field="buffer_budget_bytes",
                value=self.buffer_budget_bytes,
                required_bytes=minimum_budget,
            )
        if not MIN_SEGMENT_BYTES <= self.wal_segment_bytes <= MAX_SEGMENT_READ_BYTES:
            raise _reject(
                "wal_segment_bytes",
                self.wal_segment_bytes,
                f"a value between {MIN_SEGMENT_BYTES} and {MAX_SEGMENT_READ_BYTES} is required.",
            )

        threshold = _require_int(
            "vector_exact_scan_threshold", self.vector_exact_scan_threshold
        )
        object.__setattr__(self, "vector_exact_scan_threshold", threshold)
        if threshold < 0:
            raise _reject(
                "vector_exact_scan_threshold",
                self.vector_exact_scan_threshold,
                "a value of zero or more is required.",
            )

        for field in (
            "lease_ttl_seconds",
            "lease_timeout_seconds",
            "commit_lock_timeout_seconds",
            "reader_stall_threshold_seconds",
        ):
            object.__setattr__(
                self, field, _require_positive_number(field, getattr(self, field))
            )

        recall_target = _require_positive_number(
            "vector_recall_target", self.vector_recall_target
        )
        object.__setattr__(self, "vector_recall_target", recall_target)
        if recall_target > 1.0:
            raise _reject(
                "vector_recall_target",
                self.vector_recall_target,
                "a value greater than zero and at most one is required.",
            )

        for field, choices in (
            ("recovery_policy", RECOVERY_POLICIES),
            ("metrics", METRICS_SINKS),
            ("vector_math", VECTOR_MATH_SELECTORS),
            ("checksum", CHECKSUM_SELECTORS),
        ):
            object.__setattr__(
                self, field, _require_choice(field, getattr(self, field), choices)
            )
        self._validate_metrics_destination()

        if type(self.read_only) is not bool:
            raise _reject("read_only", self.read_only, "a boolean is required.")

    def _validate_metrics_destination(self) -> None:
        """Check the destination against the sink that will consume it (amendment A8).

        A JSON sink writes to a file and cannot invent a path; an OpenMetrics publisher listens
        on a host and port and has a sane default; the no-op sink has nowhere to send anything,
        so a destination there is a configuration mistake worth naming rather than ignoring.
        """
        destination = self.metrics_destination
        if destination is not None:
            destination = _require_text("metrics_destination", destination)
            object.__setattr__(self, "metrics_destination", destination)

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
        port_is_valid = (
            bool(separator)
            and bool(host)
            and 1 <= len(port) <= 5
            and port.isascii()
            and port.isdigit()
            and 0 <= int(port) <= 65535
        )
        if not port_is_valid:
            raise _reject(
                "metrics_destination",
                destination,
                "the OpenMetrics publisher needs a host:port with a port between 0 and 65535.",
            )

    @property
    def granularity_descriptor(self) -> str:
        """Return the self-describing granularity string every transaction record carries (TR-4)."""
        return f"hash-v1;partitions_per_table={self.partitions_per_table}"


_DATABASE_CONFIG_FIELDS: tuple[str, ...] = tuple(
    definition.name for definition in fields(DatabaseConfig)
)
"""Literal state copied at every public composition boundary before it can be consumed."""


def _canonical_database_config(value: object) -> DatabaseConfig:
    """Rebuild an exact config so even an uninitialised or tampered instance fails closed.

    ``frozen=True`` protects ordinary callers, but Python deliberately leaves
    ``object.__setattr__`` and ``object.__new__`` available. Exact type identity therefore is
    not proof that ``__post_init__`` ran or that its canonical values still occupy the slots.
    A fresh construction is both the validation seal and a value detached from later changes to
    the original object.
    """
    if type(value) is not DatabaseConfig:
        observed = _builtin_type_name(value)
        raise GrafxConfigurationError(
            f"A database operation needs an exact DatabaseConfig; got {observed}.",
            field="config",
            value=observed,
        )
    copied: dict[str, object] = {}
    for field in _DATABASE_CONFIG_FIELDS:
        try:
            copied[field] = object.__getattribute__(value, field)
        except Exception as failure:
            cause = _builtin_type_name(failure)
            raise GrafxConfigurationError(
                f"DatabaseConfig is missing validated field {field!r}; got {cause}.",
                field="config",
                missing=field,
                cause=cause,
            ) from failure
    return DatabaseConfig(**copied)  # type: ignore[arg-type]
