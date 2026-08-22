"""The default port stack: every slot filled by the adapter the configuration selects (C11, A13).

This is the half of the composition root that turns a ``DatabaseConfig`` into seven live adapters.
The properties worth pinning here are the ones no single component could check: that the build
ORDER is a dependency order rather than a mapping order, that the coordinator really receives the
settings amendment A13 fixes, that a selector binds the adapter it names, and that a build which
fails part way through releases what it had already opened.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx.adapters.clock_system import SystemClock
from okto_grafx.adapters.codec_v1 import PageCodecV1
from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
from okto_grafx.adapters.events_logging import LoggingEventSink
from okto_grafx.adapters.metrics_json import JsonMetricsSink
from okto_grafx.adapters.metrics_noop import NoOpMetricsSink
from okto_grafx.adapters.metrics_openmetrics import OpenMetricsSink
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.adapters.storage_memory import MemoryStorageDevice
from okto_grafx.adapters.vectormath_pure import PureVectorMath
from okto_grafx.api import connect
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxPortNotConfigured,
    GrafxUnsupportedOperation,
)
from okto_grafx.runtime.bootstrap import (
    BUILD_ORDER,
    PortContext,
    build_default_registry,
    coordinator_settings,
    lock_directory,
    release_ports,
)
from okto_grafx.runtime.config import DatabaseConfig
from okto_grafx.runtime.registry import PortRegistry


def _config(path: str | Path, **options: object) -> DatabaseConfig:
    """Return a configuration for a database at this path with these overrides."""
    return DatabaseConfig(path=str(path), **options)  # type: ignore[arg-type]


def _closed(device: object) -> bool:
    """Return True when a storage device refuses work because it has been closed.

    The device exposes no ``closed`` flag, so the question is put to it the way a caller would:
    a closed device refuses every door with a typed refusal that names the reason. Asserting the
    reason rather than the exception class is what makes this observe the closing rather than any
    other refusal the same class covers (A62).
    """
    try:
        device.exists("probe")  # type: ignore[attr-defined]
    except GrafxUnsupportedOperation as refusal:
        return refusal.details.get("reason") == "device_closed"
    return False


def test_every_required_slot_is_filled_by_the_default_build(tmp_path: Path) -> None:
    registry = build_default_registry(_config(tmp_path / "db"))
    try:
        # require_complete raises when a slot is empty, so reaching the next line IS the check;
        # the per-slot get() below then proves each slot holds something rather than the
        # registry merely counting its own keys.
        registry.require_complete()
        for slot in PortRegistry.REQUIRED:
            assert registry.get(slot) is not None
    finally:
        release_ports(registry)


def test_the_build_order_covers_every_required_slot_exactly_once() -> None:
    assert sorted(BUILD_ORDER) == sorted(PortRegistry.REQUIRED)
    assert len(BUILD_ORDER) == len(set(BUILD_ORDER))


def test_the_coordinator_is_built_after_the_ports_it_depends_on() -> None:
    # A13: a coordinator cannot be built from a configuration alone. The order is a dependency
    # order, so its three dependencies must be strictly earlier in it.
    position = {slot: index for index, slot in enumerate(BUILD_ORDER)}
    for dependency in ("storage", "clock", "metrics"):
        assert position[dependency] < position["coordinator"]


def test_a_factory_cannot_reach_a_port_that_has_not_been_built_yet() -> None:
    context = PortContext(config=_config(":memory:"), ports={}, lock_directory=None)
    with pytest.raises(GrafxPortNotConfigured) as failure:
        context.require("storage")
    assert failure.value.details["missing"] == ["storage"]


def test_the_memory_path_selects_the_memory_device() -> None:
    registry = build_default_registry(_config(":memory:"))
    try:
        assert isinstance(registry.get("storage"), MemoryStorageDevice)
    finally:
        release_ports(registry)


def test_a_directory_path_selects_the_local_device_at_the_configured_page_size(
    tmp_path: Path,
) -> None:
    registry = build_default_registry(_config(tmp_path / "db", page_size=1024))
    try:
        device = registry.get("storage")
        assert isinstance(device, LocalStorageDevice)
        assert device.page_size == 1024
        assert Path(device.root) == tmp_path / "db"
    finally:
        release_ports(registry)


def test_the_codec_is_built_at_the_configured_page_size(tmp_path: Path) -> None:
    registry = build_default_registry(_config(tmp_path / "db", page_size=2048))
    try:
        codec = registry.get("codec")
        assert isinstance(codec, PageCodecV1)
        assert codec.page_size == 2048
    finally:
        release_ports(registry)


def test_the_clock_and_the_event_sink_are_the_delivered_adapters(tmp_path: Path) -> None:
    registry = build_default_registry(_config(tmp_path / "db"))
    try:
        assert isinstance(registry.get("clock"), SystemClock)
        assert isinstance(registry.get("events"), LoggingEventSink)
    finally:
        release_ports(registry)


@pytest.mark.parametrize(
    ("selector", "expected", "enabled"),
    [
        ("noop", NoOpMetricsSink, False),
        ("openmetrics", OpenMetricsSink, True),
    ],
)
def test_the_metrics_selector_binds_the_sink_it_names(
    tmp_path: Path, selector: str, expected: type, enabled: bool
) -> None:
    registry = build_default_registry(_config(tmp_path / "db", metrics=selector))
    try:
        sink = registry.get("metrics")
        assert isinstance(sink, expected)
        assert sink.enabled is enabled
    finally:
        release_ports(registry)


def test_the_json_selector_binds_a_sink_writing_to_the_configured_destination(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "metrics.jsonl"
    registry = build_default_registry(
        _config(tmp_path / "db", metrics="json", metrics_destination=str(destination))
    )
    try:
        sink = registry.get("metrics")
        assert isinstance(sink, JsonMetricsSink)
        sink.publish()
        assert destination.exists()
    finally:
        release_ports(registry)


@pytest.mark.parametrize("selector", ["auto", "pure"])
def test_auto_and_pure_both_bind_the_oracle(tmp_path: Path, selector: str) -> None:
    # SPEC-VEC FR-7 makes the accelerator "selected by configuration", TR-6 and IR-2 put numpy in
    # an optional extra, and the two adapters agree only to a tolerance. A selector that bound
    # whichever adapter happened to be installed would make a ranking depend on the machine.
    registry = build_default_registry(_config(tmp_path / "db", vector_math=selector))
    try:
        adapter = registry.get("vector_math")
        assert isinstance(adapter, PureVectorMath)
        assert adapter.name == "pure"
    finally:
        release_ports(registry)


def test_the_numpy_selector_never_answers_with_the_oracle(tmp_path: Path) -> None:
    # Either the accelerator is installed and is what gets bound, or it is not and the caller is
    # told so by name. Silently substituting the oracle for an explicitly requested accelerator
    # is the one answer this selector may not give.
    try:
        registry = build_default_registry(_config(tmp_path / "db", vector_math="numpy"))
    except GrafxConfigurationError as failure:
        assert failure.details["field"] == "vector_math"
        assert "accel" in str(failure)
        return
    try:
        assert registry.get("vector_math").name == "numpy"
    finally:
        release_ports(registry)


def test_the_coordinator_carries_the_settings_the_amendment_fixes(tmp_path: Path) -> None:
    config = _config(
        tmp_path / "db",
        lease_ttl_seconds=3.0,
        reader_stall_threshold_seconds=11.0,
        commit_lock_timeout_seconds=7.0,
    )
    assert coordinator_settings(config) == {
        "ttl_seconds": 3.0,
        "owner_stall_threshold": 3.0,
        "reader_stall_threshold": 11.0,
        "section_timeout": 7.0,
    }
    registry = build_default_registry(config)
    try:
        coordinator = registry.get("coordinator")
        assert isinstance(coordinator, LocalProcessCoordinator)
        lease = coordinator.acquire_writer_lease(timeout=1.0)
        try:
            # The one setting an observer can read back through the frozen port surface.
            assert lease.ttl_seconds == 3.0
        finally:
            coordinator.release_lease(lease)
    finally:
        release_ports(registry)


def test_the_coordinator_locks_in_the_control_directory_of_the_database(tmp_path: Path) -> None:
    config = _config(tmp_path / "db")
    assert lock_directory(config) == str(tmp_path / "db" / "control")
    registry = build_default_registry(config)
    try:
        coordinator = registry.get("coordinator")
        lease = coordinator.acquire_writer_lease(timeout=1.0)
        coordinator.release_lease(lease)
        assert (tmp_path / "db" / "control").is_dir()
    finally:
        release_ports(registry)


def test_an_in_memory_database_gets_no_lock_directory() -> None:
    assert lock_directory(_config(":memory:")) is None


def test_a_build_that_fails_late_releases_the_device_it_already_opened(tmp_path: Path) -> None:
    # Storage is the FIRST slot built and the only one holding a host resource; the coordinator is
    # the LAST. A lock directory whose name is taken by a file is a real, reachable failure of
    # that last slot, so it exercises the whole window in which a device can leak.
    root = tmp_path / "db"
    root.mkdir()
    (root / "control").write_bytes(b"not a directory")

    opened: list[LocalStorageDevice] = []
    original = LocalStorageDevice.__init__

    def recording(self: LocalStorageDevice, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)  # type: ignore[arg-type]
        opened.append(self)

    LocalStorageDevice.__init__ = recording  # type: ignore[method-assign]
    try:
        with pytest.raises(GrafxConfigurationError):
            build_default_registry(_config(root))
    finally:
        LocalStorageDevice.__init__ = original  # type: ignore[method-assign]
    # A44: the device is the object created inside this call, never "the first device named X".
    assert len(opened) == 1
    assert _closed(opened[0]) is True


def test_releasing_a_registry_twice_is_harmless(tmp_path: Path) -> None:
    registry = build_default_registry(_config(tmp_path / "db"))
    release_ports(registry)
    release_ports(registry)
    assert _closed(registry.get("storage")) is True


def test_the_composition_hands_the_caller_its_own_adapter_instances_through(
    tmp_path: Path,
) -> None:
    """Identity, not type: a root that rebuilt an equivalent adapter would pass a type check.

    A caller composes its own registry when it needs THOSE objects -- a device it will inspect, a
    sink it is already scraping, a coordinator it shares with something else. Handing back an
    equivalent instance satisfies every ``isinstance`` and is still the wrong object.
    """
    registry = build_default_registry(_config(tmp_path / "db"))
    bound = {slot: registry.get(slot) for slot in PortRegistry.REQUIRED}
    try:
        database = connect(tmp_path / "db", registry=registry)
        try:
            assert database.storage is bound["storage"]
            assert database.clock is bound["clock"]
            assert database.codec is bound["codec"]
            assert database.metrics is bound["metrics"]
            assert database.events is bound["events"]
            assert database.vector_math is bound["vector_math"]
            assert database.coordinator is bound["coordinator"]
            # The engines built on top must reach the same objects, not copies of them.
            assert database.pool.storage is bound["storage"]
        finally:
            database.close()
    finally:
        release_ports(registry)


def test_the_default_build_takes_every_adapter_from_the_adapters_package(
    tmp_path: Path,
) -> None:
    # The composition root is the only module allowed to know the concrete adapters, and every
    # slot must come from there rather than from a double or a local definition. Asserted by
    # MODULE so a component stays free to rename its class.
    registry = build_default_registry(_config(tmp_path / "db"))
    try:
        for slot in PortRegistry.REQUIRED:
            module = type(registry.get(slot)).__module__
            assert module.startswith("okto_grafx.adapters."), f"{slot} came from {module}"
    finally:
        release_ports(registry)
