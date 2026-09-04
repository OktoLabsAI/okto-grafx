"""The composition root (CONTRACT.md section 5, amendment A13).

This is the only place allowed to know both the pure core and the concrete adapters. It builds
the default adapters for a configuration in dependency order, verifies that every port slot is
filled, and only then assembles the engine. Nothing above this module ever imports an adapter.

Ports are not independent. A coordinator cannot be built from a configuration alone: it needs
the storage device and the clock that were already built, and the one thing no port can express
-- the real directory that holds its advisory lock files. So a factory receives a
:class:`PortContext` rather than a bare configuration, and the slots are built in
:data:`BUILD_ORDER` so a later factory can ask for an earlier instance by name.

C0 owns the factory shape and the configuration mapping; C11 owns the wiring (A13). The adapter
table below is that wiring, and the engine assembly it feeds lives in
:mod:`okto_grafx.api.assembly`. A registry that is still missing a slot fails closed with
GrafxPortNotConfigured, which is the behaviour BR-8 and G5 require, and a build that fails part
way through releases what it had already built rather than leaving a device holding descriptors.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from okto_grafx.adapters.clock_system import SystemClock
from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.adapters.events_logging import LoggingEventSink
from okto_grafx.adapters.metrics_json import JsonMetricsSink, RotatingFileWriter
from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.metrics_openmetrics import OpenMetricsSink
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.adapters.storage_read_only import ReadOnlyStorageDevice
from okto_grafx.adapters.checksum_pure import PureCrc32c
from okto_grafx.domain.page.checksum import crc32c_implementation
from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.domain import errors as grafx_errors
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxError,
    GrafxPortNotConfigured,
    GrafxQuarantineError,
    GrafxUnsupportedOperation,
)
from okto_grafx.engine.public_views import QuarantineView, _quarantine_view
from okto_grafx.engine.quarantine import QuarantineStore
from okto_grafx.runtime.config import (
    DEFAULT_OPENMETRICS_DESTINATION,
    MEMORY_PATH,
    DatabaseConfig,
    _canonical_database_config,
)
from okto_grafx.runtime.registry import PortRegistry, _snapshot_port_registry

if TYPE_CHECKING:
    from okto_grafx.engine.database import Database

__all__ = [
    "BUILD_ORDER",
    "CONTROL_DIRECTORY_NAME",
    "PortContext",
    "PortFactory",
    "QuarantineInventoryReader",
    "build_clock",
    "build_codec",
    "build_coordinator",
    "build_events",
    "build_metrics",
    "build_storage",
    "build_vector_math",
    "install_checksum",
    "coordinator_settings",
    "lock_directory",
    "metrics_destination",
    "release_ports",
    "build_default_registry",
    "open_database",
    "open_quarantine_inventory",
]

CONTROL_DIRECTORY_NAME: str = "control"
"""The subdirectory of a database that holds the lease, the reader registrations and the locks.

Careful across the C0/C3 seam: this is the *name* of that subdirectory, and
:func:`lock_directory` joins it onto the database path to produce a host path. The coordinator
adapter has a parameter also called ``control_directory``, and it means something different --
a prefix inside the storage namespace, defaulting to the same word ``"control"``. The host path
belongs in the adapter's ``lock_directory=`` parameter; passing it into ``control_directory=``
would push an absolute host path into the device namespace. :attr:`PortContext.lock_directory`
carries the host path under the name the adapter expects.
"""

BUILD_ORDER: tuple[str, ...] = (
    "storage",
    "clock",
    "codec",
    "metrics",
    "events",
    "vector_math",
    "coordinator",
)
"""Dependency order, not mapping order (A13).

Storage and the clock come first because the coordinator is built from them; metrics precede the
coordinator because it reports lease waits through the sink; the coordinator comes last because
it depends on the most. Every required slot appears exactly once.
"""


@dataclass(frozen=True, slots=True)
class PortContext:
    """What a port factory is given: the configuration, the ports already built, and the paths.

    ``ports`` holds only the slots built before this one, in :data:`BUILD_ORDER`, so a factory
    cannot accidentally depend on a port that does not exist yet -- asking for it raises rather
    than handing back None.
    """

    config: DatabaseConfig
    ports: Mapping[str, object]
    lock_directory: str | None

    def require(self, slot: str) -> object:
        """Return an already-built port, refusing a dependency that is not ready yet."""
        try:
            return self.ports[slot]
        except KeyError:
            raise GrafxPortNotConfigured(
                f"Port slot {slot!r} is needed by a factory but has not been built yet; "
                f"available so far: {', '.join(self.ports) or 'nothing'}.",
                missing=[slot],
                available=list(self.ports),
            ) from None


_DetachedCloseOutcome = tuple[str, str, str, bool, bool]
"""Only inert builtins retained from a close failure.

The fields are category, public type name, message, retryability and inconclusive status.  The
original exception is deliberately absent: its traceback would retain the storage frame and its
``args`` or arbitrary attributes could themselves be capabilities.
"""

_DECLARED_GRAFX_ERROR_TYPES: tuple[type[GrafxError], ...] = tuple(
    error_type
    for name in grafx_errors.__all__
    if isinstance((error_type := getattr(grafx_errors, name)), type)
    and issubclass(error_type, GrafxError)
)
"""The engine-owned error classes whose base fields are safe to detach without dispatch."""


class _DetachedQuarantineCloseFailure(GrafxError):
    """A fresh typed refusal reconstructed without the source exception or traceback."""

    def __init__(self, outcome: _DetachedCloseOutcome) -> None:
        category, error_type, message, retryable, inconclusive = outcome
        super().__init__(
            message,
            retryable=retryable,
            close_error_type=error_type,
            inconclusive=inconclusive,
        )
        self.code = category
        self.error_type = error_type
        self.inconclusive = inconclusive

    def to_dict(self) -> dict[str, object]:
        """Keep the public taxonomy captured at the source while remaining detached."""
        document = super().to_dict()
        document["type"] = self.error_type
        return document


def _detach_close_failure(failure: Exception) -> _DetachedCloseOutcome:
    """Copy routing facts without invoking any method supplied by the caught object."""
    try:
        failure_type = type(failure)
        if not any(
            failure_type is declared for declared in _DECLARED_GRAFX_ERROR_TYPES
        ):
            return "", "", "A foreign close failure was safely detached.", False, False

        # Every declared class inherits GrafxError's concrete instance fields.  Direct base
        # lookup plus exact builtin checks avoids __str__, __repr__, custom mappings and values
        # hidden in ``args``; an injected subclass is deliberately treated as foreign above.
        captured_message = _declared_error_field(failure, "message")
        message = (
            captured_message
            if type(captured_message) is str
            else "A Grafx close failure had no safe text message."
        )
        captured_code = _declared_error_field(failure, "code")
        category = captured_code if type(captured_code) is str else "grafx_error"
        retryable = _declared_error_field(failure, "retryable") is True
        captured_details = _declared_error_field(failure, "details")
        inconclusive = (
            type(captured_details) is dict
            and dict.get(captured_details, "inconclusive") is True
        )
        error_type = failure_type.__name__
        if type(error_type) is not str:
            error_type = "GrafxError"
        return category, error_type, message, retryable, inconclusive
    except BaseException:
        return "", "", "A close failure could not be safely detached.", False, False


def _declared_error_field(failure: GrafxError, name: str) -> object | None:
    """Read an engine-owned base field with a no-throw fallback for malformed instances."""
    try:
        return object.__getattribute__(failure, name)
    except BaseException:
        return None


class QuarantineInventoryReader:
    """A capability-safe immutable view of just one database quarantine.

    A normal read-only database still participates in cross-process coordination, and the local
    coordinator's advisory lock names are persistent filesystem objects.  A forensic inventory
    must not create even those objects in the namespace it is diagnosing.  This smaller
    composition therefore captures the public :class:`QuarantineView` over a read-only storage
    capability; it never exposes the raw device or constructs the coordinator, WAL, recovery
    manager or database engine.
    """

    __slots__ = ("__close_outcome", "__closed", "path", "quarantine")

    def __init__(
        self,
        path: str,
        quarantine: QuarantineView,
        close_outcome: _DetachedCloseOutcome | None = None,
    ) -> None:
        """Retain only values detached from the storage that produced the snapshot."""
        self.path = path
        self.quarantine = quarantine
        self.__close_outcome = close_outcome
        self.__closed = False

    def close(self) -> None:
        """Report the source close outcome once; no storage capability is retained."""
        if self.__closed:
            return
        self.__closed = True
        outcome = self.__close_outcome
        self.__close_outcome = None
        if outcome is None:
            return
        if outcome[0] == GrafxCorruptionDetected.code:
            raise GrafxCorruptionDetected(outcome[2])
        if outcome[0]:
            raise _DetachedQuarantineCloseFailure(outcome)
        raise RuntimeError(outcome[2])


def _close_quarantine_inventory_storage_quietly(
    storage: LocalStorageDevice,
) -> None:
    """Release a half-built reader without replacing the failure that caused cleanup."""
    try:
        storage.close_read_only()
    except BaseException:
        pass


def open_quarantine_inventory(config: DatabaseConfig) -> QuarantineInventoryReader:
    """Open only the capabilities needed to inventory persisted quarantine evidence.

    The caller must explicitly request a read-only configuration.  Filesystem storage is opened
    with ``create_root=False`` and then hidden behind :class:`ReadOnlyStorageDevice`, so neither a
    missed call site nor cleanup can mutate the database.  The no-op metrics sink is deliberate:
    an evidence inventory has no hot-path measurement to publish and must not acquire an external
    output capability merely because it is inspecting damage.
    """
    config = _canonical_database_config(config)
    if not config.read_only:
        raise GrafxConfigurationError(
            "A quarantine inventory reader requires an explicitly read-only configuration.",
            field="read_only",
            value=False,
        )
    if config.path == MEMORY_PATH:
        raise GrafxUnsupportedOperation(
            "An in-memory database has no persisted quarantine namespace to inventory.",
            component="quarantine",
            path=config.path,
            read_only=True,
        )

    storage = LocalStorageDevice(
        config.path,
        page_size=config.page_size,
        create_root=False,
        descriptor_revalidation=config.descriptor_revalidation,
    )
    try:
        # This is the authoritative identity classification expressed without constructing a
        # coordinator.  A foreign file merely named ``grafx.meta``, incomplete first-open state,
        # configuration that disagrees with the durable layout, or divergent completion evidence
        # must never be reported as a conclusively empty quarantine.
        from okto_grafx.api.assembly import _observe_default_read_only_identity

        identity = _observe_default_read_only_identity(
            config,
            storage,
            PageCodecV1(config.page_size),
        )
        quarantine = _quarantine_view(
            QuarantineStore(
                ReadOnlyStorageDevice(storage),
                SystemClock(),
                NoOpMetricsSink(),
            )
        )
        confirmed = _observe_default_read_only_identity(
            config,
            storage,
            PageCodecV1(config.page_size),
        )
        if confirmed != identity:
            raise GrafxQuarantineError(
                "The database identity changed while quarantine was being inventoried, so the "
                "snapshot cannot be certified.",
                component="quarantine",
                path=config.path,
                conclusive=False,
                inconclusive=True,
                retryable=True,
            )
    except BaseException:
        _close_quarantine_inventory_storage_quietly(storage)
        raise

    # ``_quarantine_view`` owns only exact immutable values.  Close the concrete device before
    # returning so no public attribute, bound method or closure can recover a writable storage
    # capability from the reader.  A normal close failure is transported as inert outcome state:
    # the command can then preserve already-captured findings while still reporting the problem.
    close_outcome: _DetachedCloseOutcome | None = None
    try:
        storage.close_read_only()
    except Exception as failure:
        try:
            close_outcome = _detach_close_failure(failure)
        except BaseException:
            close_outcome = (
                "",
                "",
                "A close failure could not be safely detached.",
                False,
                False,
            )
        finally:
            _close_quarantine_inventory_storage_quietly(storage)
    except BaseException:
        _close_quarantine_inventory_storage_quietly(storage)
        raise
    return QuarantineInventoryReader(
        path=config.path,
        quarantine=quarantine,
        close_outcome=close_outcome,
    )


PortFactory = Callable[[PortContext], object]
"""How a slot is built: one context in, one adapter instance out (A13)."""


def build_storage(context: PortContext) -> object:
    """Build the storage device a configuration selects (C2).

    ``":memory:"`` selects the in-memory device, which has the same transactional semantics as
    the local one (SPEC-M1 FR-1); every other path selects the directory-backed device, which
    creates the directory when it does not exist yet.
    """
    config = context.config
    if config.path == MEMORY_PATH:
        return MemoryStorageDevice(page_size=config.page_size)
    return LocalStorageDevice(
        config.path,
        page_size=config.page_size,
        max_open_files=config.max_open_files,
        create_root=not config.read_only,
        descriptor_revalidation=config.descriptor_revalidation,
    )


def build_clock(context: PortContext) -> object:
    """Build the clock (C3): monotonic for liveness, wall for human-facing stamps only."""
    return SystemClock()


def build_codec(context: PortContext) -> object:
    """Build the selected byte-identical page codec at this database's page size."""

    selector = context.config.codec
    if selector == "pure":
        return PageCodecV1(context.config.page_size)
    try:
        from okto_grafx.adapters.codec_numpy import NumpyPageCodecV1
    except ImportError as failure:
        raise GrafxConfigurationError(
            "The NumPy page codec needs the optional 'accel' extra; install "
            "okto-grafx[accel] or configure codec='pure'.",
            field="codec",
            value=selector,
        ) from failure
    return NumpyPageCodecV1(context.config.page_size)


def build_metrics(context: PortContext) -> object:
    """Build the metrics sink a configuration selects (C8, SPEC-M1 FR-14, amendment A8).

    ``"noop"`` allocates and formats nothing on a hot path, ``"openmetrics"`` records into the
    aggregator the publisher exposes at ``GET /metrics``, and ``"json"`` writes documents to the
    file the configuration names. The destination of the publisher is resolved by the caller of
    this factory, because a listening socket is a resource with a lifetime and a port is not.
    """
    config = context.config
    if config.metrics == "noop":
        return NoOpMetricsSink()
    if config.metrics == "openmetrics":
        return OpenMetricsSink()
    destination = metrics_destination(config)
    if destination is None:  # pragma: no cover - DatabaseConfig already refuses this
        raise GrafxConfigurationError(
            "The JSON metrics sink writes to a file, so a destination is required.",
            field="metrics_destination",
            value=None,
        )
    return JsonMetricsSink(RotatingFileWriter(destination))


def build_events(context: PortContext) -> object:
    """Build the event sink (C8): sanitised, bounded records on a standard library logger."""
    return LoggingEventSink()


def build_vector_math(context: PortContext) -> object:
    """Build the vector math adapter a configuration selects (C9, SPEC-VEC FR-7, A52).

    ``"pure"`` and ``"auto"`` both bind the pure oracle, and that is deliberate. SPEC-VEC FR-7
    describes the accelerator as *selected by configuration*, TR-6 and IR-2 put numpy in the
    optional ``[accel]`` extra, and the two adapters agree only to a stated tolerance -- so a
    selector that silently bound whichever adapter happened to be installed would make the
    ranking of a query depend on the machine it ran on. ``"auto"`` therefore means "let the
    composition root choose", and the composition root chooses the answer that is the same
    everywhere. ``"numpy"`` binds the accelerator and refuses when the extra is not installed,
    rather than falling back to something the caller did not ask for.
    """
    selector = context.config.vector_math
    if selector != "numpy":
        return PureVectorMath()
    try:
        from okto_grafx.adapters.vectormath_numpy import NumpyVectorMath
    except ImportError as failure:
        raise GrafxConfigurationError(
            "The numpy vector math adapter needs the optional 'accel' extra; install "
            "okto-grafx[accel] or configure vector_math='pure'.",
            field="vector_math",
            value=selector,
        ) from failure
    return NumpyVectorMath()


def install_checksum(config: DatabaseConfig) -> str:
    """Install the CRC-32C implementation this configuration selects, and return its name.

    This is not a port. The checksum is installed process-wide because every component that
    computes one must compute the SAME one -- a page written by the pool and verified by the
    verifier is one answer, not two -- and a value injected per database would let two open
    databases in one process disagree about what a byte range hashes to.

    Installing is safe in a way that binding a vector math adapter is not, and that is why
    ``"auto"`` accelerates here and does not there. The native path is a closed provider list,
    replays the acceptance corpus through :func:`crc32c_reference`, and contains every runtime
    result as an exact u32. Arbitrary injected callables take the stricter per-call oracle path;
    the closed-list trust boundary is what lets the shipped native selector remain fast.

    ``"native"`` refuses when no provider is installed rather than falling back, for the reason
    every selector in this module refuses: a caller who asked for the accelerator and silently
    got the reference would measure the wrong thing and report it as the product's speed.
    """
    config = _require_database_config(config)
    selector = config.checksum
    if selector == "pure":
        return _install(PureCrc32c())
    try:
        from okto_grafx.adapters.checksum_native import NativeCrc32c
    except (
        ImportError
    ) as failure:  # pragma: no cover - the module is part of this package
        if selector == "native":
            raise GrafxConfigurationError(
                "The native checksum adapter could not be imported.",
                field="checksum",
                value=selector,
            ) from failure
        return _install(PureCrc32c())
    try:
        return _install(NativeCrc32c())
    except ImportError as failure:
        if selector == "native":
            raise GrafxConfigurationError(
                "The native CRC-32C adapter needs a provider from the optional 'accel' extra; "
                "install okto-grafx[accel] or configure checksum='pure'.",
                field="checksum",
                value=selector,
            ) from failure
        # "auto" asked for the fastest correct answer, and the reference is a correct answer.
        return _install(PureCrc32c())
    except GrafxConfigurationError:
        if selector == "native":
            raise
        # An installed but invalid optional provider is no reason to abandon the safe reference.
        return _install(PureCrc32c())
    except GrafxError:
        # A provider that already classified its own failure keeps that exact public answer.
        raise
    except Exception as failure:
        cause = _builtin_type_name(failure)
        if selector == "native":
            raise GrafxConfigurationError(
                f"The native CRC-32C provider failed with {cause} during validation.",
                field="checksum",
                value=selector,
                cause=cause,
            ) from failure
        # A broken optional provider cannot make the safe, always-available reference unusable.
        return _install(PureCrc32c())


def _install(adapter: object) -> str:
    """Install one checksum adapter and return the name that is now IN EFFECT.

    The adapters' own ``install()`` returns the name it REPLACED, and says so; this door promises
    the name it installed. While the reference was the only implementation that could ever be in
    effect, the two were the same string and nothing could tell them apart -- so the first machine
    with a native provider installed made ``install_checksum(checksum='pure')`` answer ``'native'``.
    Two values that must be different were secretly one.
    """
    adapter.install()  # type: ignore[attr-defined]
    return crc32c_implementation()


def build_coordinator(context: PortContext) -> object:
    """Build the process coordinator (C3) over the storage device and clock already built (A13).

    The lock directory is the one thing no port can express -- ``<database path>/control`` for a
    real file system, None for ``":memory:"``, which selects the process-wide sections that mode
    requires. The four timing settings are amendment A13's mapping, produced by
    :func:`coordinator_settings` so the composition root cannot drift from the amendment.
    """
    storage = context.require("storage")
    clock = context.require("clock")
    metrics = context.require("metrics")
    return LocalProcessCoordinator(
        storage,  # type: ignore[arg-type]
        clock,  # type: ignore[arg-type]
        lock_directory=context.lock_directory,
        metrics=metrics,  # type: ignore[arg-type]
        **coordinator_settings(context.config),
    )


_DEFAULT_PORT_FACTORIES: Mapping[str, PortFactory] = MappingProxyType(
    {
        "storage": build_storage,
        "clock": build_clock,
        "codec": build_codec,
        "metrics": build_metrics,
        "events": build_events,
        "vector_math": build_vector_math,
        "coordinator": build_coordinator,
    }
)
"""Slot to adapter factory: "codec" (C1), "storage" (C2), "clock" and "coordinator" (C3),
"metrics" and "events" (C8), "vector_math" (C9).

The order the slots are BUILT in is :data:`BUILD_ORDER`, not the order they appear here.
"""


def lock_directory(config: DatabaseConfig) -> str | None:
    """Return the host directory the coordinator locks in, or None when there is no file system.

    A13: the coordinator locks in ``<database path>/control``. An in-memory database has no such
    directory, and passing None is what selects the process-wide sections that mode requires.

    The name matters. This is the value for the adapter's ``lock_directory=`` parameter, not for
    its ``control_directory=`` parameter -- see :data:`CONTROL_DIRECTORY_NAME` for why confusing
    the two would put a host path inside the storage namespace.
    """
    if config.path == MEMORY_PATH:
        return None
    return os.path.join(config.path, CONTROL_DIRECTORY_NAME)


def coordinator_settings(config: DatabaseConfig) -> dict[str, float]:
    """Return the coordinator keyword arguments a configuration implies (A13, verbatim).

    The mapping lives here rather than in prose so the composition root cannot drift from the
    amendment: ``owner_stall_threshold`` deliberately follows the lease time to live, because an
    owner that has not renewed within its own lease is the definition of a stalled owner.
    """
    return {
        "ttl_seconds": config.lease_ttl_seconds,
        "owner_stall_threshold": config.lease_ttl_seconds,
        "reader_stall_threshold": config.reader_stall_threshold_seconds,
        "section_timeout": config.commit_lock_timeout_seconds,
    }


def metrics_destination(config: DatabaseConfig) -> str | None:
    """Return where the selected metrics sink writes, applying the default A8 declares.

    A8 gives the OpenMetrics publisher a default of ``127.0.0.1:0`` -- an ephemeral local port,
    so a database never fails to open because a fixed port was taken. The configuration stores
    None for it, because None is what "the caller did not choose" means; resolving that None into
    the default is a composition decision, and this is the composition root. The JSON sink has no
    default: its destination is required, and the configuration has already refused a build
    without one. The no-op sink has nowhere to write.
    """
    if config.metrics == "noop":
        return None
    if config.metrics_destination is not None:
        return config.metrics_destination
    if config.metrics == "openmetrics":
        return DEFAULT_OPENMETRICS_DESTINATION
    return None


def release_ports(ports: PortRegistry) -> None:
    """Release every adapter of a registry that owns a host resource, quietly.

    Only a device holds anything: it caches descriptors and may hold a pending deletion queue.
    Failures are swallowed on purpose -- this runs while an earlier failure is already on its way
    to the caller, and replacing that failure with a failure of the closing path would hide the
    reason the composition is being abandoned at all.
    """
    _release_ports(ports, observational_storage=False)


def _release_ports(ports: PortRegistry, *, observational_storage: bool) -> None:
    """Release built adapters, optionally closing storage without writable housekeeping."""
    for slot in reversed(BUILD_ORDER):
        try:
            instance = ports.get(slot)
        except Exception:
            continue
        closer = (
            getattr(instance, "close_read_only", None)
            if observational_storage and slot == "storage"
            else None
        )
        if closer is None or not callable(closer):
            closer = getattr(instance, "close", None)
        if closer is None or not callable(closer):
            continue
        try:
            closer()
        except BaseException:
            continue


def build_default_registry(config: DatabaseConfig) -> PortRegistry:
    """Build the default adapters for this configuration and return a complete registry.

    Slots are built in :data:`BUILD_ORDER` and each factory receives the instances built before
    it. Raises GrafxPortNotConfigured, listing every slot that stayed empty, when the current
    build cannot provide a default for all of them.

    A build that fails part way through releases what it had already built. Without that, a
    configuration error in a late slot would leave the storage device of a half-built database
    holding descriptors -- and on Windows a held descriptor is exactly what stops the next
    attempt from publishing over the same names.
    """
    config = _require_database_config(config)
    install_checksum(config)
    registry = PortRegistry()
    built: dict[str, object] = {}
    try:
        for slot in BUILD_ORDER:
            factory = _DEFAULT_PORT_FACTORIES.get(slot)
            if factory is None:
                continue
            if (
                slot == "coordinator"
                and config.read_only
                and config.path != MEMORY_PATH
            ):
                # The coordinator constructor creates ``control/`` for its advisory locks. A
                # default observational open must first prove that the path carries Grafx-owned
                # evidence; otherwise even the expected "there is no database" refusal would
                # claim an empty or foreign directory. The assembly owns this classification and
                # repeats it under first-open, so the bootstrap imports rather than restates it.
                from okto_grafx.api.assembly import _preflight_default_read_only_storage

                _preflight_default_read_only_storage(
                    config,
                    built["storage"],  # type: ignore[arg-type]
                    built["codec"],  # type: ignore[arg-type]
                )
            context = PortContext(
                config=config,
                ports=MappingProxyType(dict(built)),
                lock_directory=lock_directory(config),
            )
            instance = factory(context)
            registry.bind(slot, instance)
            built[slot] = instance
        registry.require_complete()
    except GrafxError:
        # A47: the class a factory chose, and the retryable detail it carries, are what a
        # caller switches on. A Grafx failure leaves this guard exactly as it arrived.
        _release_ports(registry, observational_storage=config.read_only)
        raise
    except Exception as failure:
        # An adapter constructor is ordinary Python and can raise ordinary Python: a path
        # holding a NUL byte reaches os.makedirs and comes back as a bare ValueError.
        # Section 2 and DoD item 5 say only Grafx types leave a public door, and this door
        # is under open_database. The assembly converts foreign exceptions and this build
        # did not -- one invariant kept in two places with only one of them holding it
        # (A66).
        _release_ports(registry, observational_storage=config.read_only)
        cause = _builtin_type_name(failure)
        raise GrafxConfigurationError(
            f"A port could not be built for the database at {config.path!r}: an adapter "
            f"raised {cause}.",
            field="ports",
            path=config.path,
            cause=cause,
        ) from failure
    except BaseException:
        # KeyboardInterrupt and SystemExit are not failures of this build and are never
        # converted; what was already built is still released on the way past.
        _release_ports(registry, observational_storage=config.read_only)
        raise
    return registry


def open_database(
    config: DatabaseConfig, *, registry: PortRegistry | None = None
) -> Database:
    """Open a database against the given configuration.

    With no registry the default adapters are built for the configuration; with a registry the
    caller owns the composition. Either way every required port slot is verified before the
    engine is assembled, so an incomplete composition refuses to start.

    Ownership decides what a later ``close()`` releases: adapters built here are released with
    the database, adapters the caller bound are left alone, because a caller that composed its
    own registry may be using those adapters for something else.

    An assembly that fails releases what it opened, and it does so in ONE place -- the assembly's
    own guard, which is the only one that also knows about the resources no port slot holds. A
    second release here would make neither observable on its own (A67).
    """
    config = _require_database_config(config)
    owns_ports = registry is None
    if registry is None:
        ports = build_default_registry(config)
    else:
        ports = _require_port_registry(registry)
        # CRC-32C is deliberately process-global rather than a port. A custom port registry
        # replaces the seven adapters, not this explicit configuration choice.
        install_checksum(config)
    ports.require_complete()
    return _assemble_database(config, ports, owns_ports=owns_ports)


def _builtin_type_name(value: object) -> str:
    """Name a caller value without executing a descriptor on its metaclass."""
    value_type = type(value)
    declared = type.__dict__["__name__"].__get__(value_type, type(value_type))
    return str.__str__(declared)


def _require_database_config(value: object) -> DatabaseConfig:
    """Return a fresh validated snapshot, even when an exact instance was forged or changed."""
    return _canonical_database_config(value)


def _require_port_registry(value: object) -> PortRegistry:
    """Return a statically revalidated snapshot of the caller's exact registry."""
    return _snapshot_port_registry(value)


def _assemble_database(
    config: DatabaseConfig, ports: PortRegistry, *, owns_ports: bool = False
) -> Database:
    """Wire the engine on top of a complete port registry (amendment A13: C11 owns the wiring).

    The wiring itself lives in :mod:`okto_grafx.api.assembly`, which is C11's own path, and is
    imported here rather than at module scope so the import arrow keeps pointing from the public
    facade down into the runtime: the facade imports this module eagerly, and this module reaches
    back only while a database is actually being opened.
    """
    from okto_grafx.api.assembly import assemble_database

    return assemble_database(config, ports, owns_ports=owns_ports)
