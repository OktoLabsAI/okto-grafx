"""The composition root refuses to start on an incomplete composition (BR-8, guideline G5)."""

from __future__ import annotations

import inspect
from pathlib import Path
from types import MappingProxyType

import pytest

from okto_grafx.api.assembly import assemble_database
from okto_grafx.adapters.storage_local import LocalStorageDevice
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxDeviceFull,
    GrafxPortNotConfigured,
)
from okto_grafx.runtime import bootstrap
from okto_grafx.runtime.bootstrap import (
    BUILD_ORDER,
    CONTROL_DIRECTORY_NAME,
    PortContext,
    lock_directory,
    coordinator_settings,
)
from okto_grafx.domain.page.checksum import (
    crc32c,
    crc32c_implementation,
    crc32c_reference,
)
from okto_grafx.runtime.config import DEFAULT_OPENMETRICS_DESTINATION, DatabaseConfig
from okto_grafx.runtime.registry import PortRegistry


def test_building_the_default_registry_fills_every_required_slot() -> None:
    # The first end-to-end assembly in this build. Until C11 the table was empty and this call
    # could only fail; it now returns a complete registry of shipped adapters, so the assertion
    # is that the composition root actually composes rather than that it refuses to.
    registry = bootstrap.build_default_registry(DatabaseConfig(path=":memory:"))
    try:
        for slot in PortRegistry.REQUIRED:
            port = registry.get(slot)
            assert port is not None, slot
            # A default build must reach for a shipped adapter, never a test double: the module
            # is asserted rather than the class name so C1-C9 stay free to rename their types.
            assert type(port).__module__.startswith("okto_grafx.adapters"), (
                slot,
                type(port),
            )
    finally:
        bootstrap.release_ports(registry)


def test_the_local_storage_descriptor_budget_comes_from_database_config(
    tmp_path: Path,
) -> None:
    registry = bootstrap.build_default_registry(
        DatabaseConfig(path=str(tmp_path / "db"), max_open_files=73)
    )
    try:
        storage = registry.get("storage")
        assert type(storage) is LocalStorageDevice
        assert storage._max_open_files == 73
    finally:
        bootstrap.release_ports(registry)


@pytest.mark.parametrize("mode", ("strict", "generation"))
def test_descriptor_revalidation_mode_reaches_the_local_storage(
    tmp_path: Path, mode: str
) -> None:
    registry = bootstrap.build_default_registry(
        DatabaseConfig(
            path=str(tmp_path / mode),
            descriptor_revalidation=mode,
        )
    )
    try:
        storage = registry.get("storage")
        assert type(storage) is LocalStorageDevice
        assert storage.descriptor_revalidation == mode
    finally:
        bootstrap.release_ports(registry)


def test_the_identity_lease_size_reaches_the_transaction_manager() -> None:
    database = bootstrap.open_database(
        DatabaseConfig(path=":memory:", identity_lease_size=17)
    )
    try:
        assert database._transactions._identity_lease_size == 17
    finally:
        database.close()


def test_building_the_default_registry_fails_closed_on_every_unfilled_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fail-closed used to be observable for free, because the table was empty and every slot was
    # missing. The table is full now, so the gap has to be made deliberately -- and two slots go
    # rather than one, because the contract is that the error names EVERY unfilled slot instead
    # of stopping at the first. Both chosen from the leaves of A13's order: nothing is built
    # from them, so their absence cannot fail the build earlier for a different reason.
    reduced = {
        slot: factory
        for slot, factory in bootstrap._DEFAULT_PORT_FACTORIES.items()
        if slot not in ("vector_math", "events")
    }
    monkeypatch.setattr(bootstrap, "_DEFAULT_PORT_FACTORIES", MappingProxyType(reduced))

    with pytest.raises(GrafxPortNotConfigured) as raised:
        bootstrap.build_default_registry(DatabaseConfig(path=":memory:"))
    assert raised.value.details["missing"] == ["vector_math", "events"]


def test_open_database_without_a_registry_builds_the_defaults(tmp_path: Path) -> None:
    # No registry supplied, so the composition root builds all seven defaults and opens on them.
    # Written against a real path rather than ":memory:" so the storage adapter takes its file
    # path, and closed in a finally because a descriptor left open on Windows is what stops the
    # next test from publishing over the same names.
    database = bootstrap.open_database(
        DatabaseConfig(path=str(tmp_path / "graph.okto"))
    )
    try:
        assert not database.closed
    finally:
        database.close()
    assert database.closed


def test_open_database_with_an_incomplete_registry_lists_the_gaps(
    fake_ports: dict[str, object],
) -> None:
    registry = PortRegistry()
    registry.bind("storage", fake_ports["storage"])
    registry.bind("clock", fake_ports["clock"])
    registry.bind("coordinator", fake_ports["coordinator"])

    with pytest.raises(GrafxPortNotConfigured) as raised:
        bootstrap.open_database(DatabaseConfig(path=":memory:"), registry=registry)

    assert raised.value.details["missing"] == [
        "codec",
        "metrics",
        "vector_math",
        "events",
    ]


@pytest.mark.parametrize(
    "registry",
    [object(), pytest.param(type("R", (PortRegistry,), {})(), id="subclass")],
)
def test_open_database_refuses_anything_but_the_exact_registry(
    registry: object,
) -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        bootstrap.open_database(  # type: ignore[arg-type]
            DatabaseConfig(path=":memory:"), registry=registry
        )
    assert raised.value.details["field"] == "registry"


class _SkippedConfig(DatabaseConfig):
    def __post_init__(self) -> None:
        """Deliberately skip every invariant of the frozen base dataclass."""


def test_bootstrap_refuses_a_config_subclass_that_skipped_validation() -> None:
    skipped = _SkippedConfig(path="", page_size=3)
    with pytest.raises(GrafxConfigurationError) as raised:
        bootstrap.open_database(skipped)
    assert raised.value.details["field"] == "config"


def test_bootstrap_revalidates_even_an_exact_config_instance() -> None:
    tampered = DatabaseConfig(path=":memory:", checksum="pure")
    object.__setattr__(tampered, "checksum", "bogus")
    with pytest.raises(GrafxConfigurationError) as invalid:
        bootstrap.open_database(tampered)
    assert invalid.value.details["field"] == "checksum"

    uninitialised = object.__new__(DatabaseConfig)
    with pytest.raises(GrafxConfigurationError) as missing:
        bootstrap.open_database(uninitialised)
    assert missing.value.details["field"] == "config"


def test_bootstrap_revalidates_even_an_exact_registry_instance(
    complete_registry: PortRegistry,
) -> None:
    uninitialised = object.__new__(PortRegistry)
    with pytest.raises(GrafxConfigurationError) as missing:
        bootstrap.open_database(DatabaseConfig(path=":memory:"), registry=uninitialised)
    assert missing.value.details["field"] == "registry"

    corrupted = PortRegistry()
    corrupted._bindings = object()  # type: ignore[assignment]
    with pytest.raises(GrafxConfigurationError) as invalid:
        bootstrap.open_database(DatabaseConfig(path=":memory:"), registry=corrupted)
    assert invalid.value.details["field"] == "registry"

    class HostileMeta(type):
        @property
        def __name__(cls) -> str:
            raise RuntimeError("caller type name ran")

    class HostileStorage(metaclass=HostileMeta):
        pass

    complete_registry._bindings["storage"] = HostileStorage()
    with pytest.raises(GrafxConfigurationError) as hostile:
        bootstrap.open_database(
            DatabaseConfig(path=":memory:"), registry=complete_registry
        )
    assert hostile.value.details["field"] == "registry"


def test_a_hostile_adapter_exception_is_contained_without_stringifying_it(
    complete_registry: PortRegistry,
) -> None:
    class HostileFailure(Exception):
        def __str__(self) -> str:
            raise RuntimeError("exception __str__ escaped")

    marker = HostileFailure()

    class FailingClock:
        def monotonic(self) -> float:
            raise marker

        def wall(self) -> float:
            raise marker

    complete_registry.bind("clock", FailingClock())
    with pytest.raises(GrafxConfigurationError) as raised:
        bootstrap.open_database(
            DatabaseConfig(path=":memory:"), registry=complete_registry
        )
    assert raised.value.details["field"] == "ports"
    assert raised.value.details["cause"] == "HostileFailure"
    assert raised.value.__cause__ is marker


def test_direct_assembly_applies_the_same_exact_boundary_guards(
    complete_registry: PortRegistry,
) -> None:
    skipped = _SkippedConfig(path="", page_size=3)
    with pytest.raises(GrafxConfigurationError) as bad_config:
        assemble_database(skipped, complete_registry)
    assert bad_config.value.details["field"] == "config"

    class RegistrySubclass(PortRegistry):
        pass

    with pytest.raises(GrafxConfigurationError) as bad_registry:
        assemble_database(DatabaseConfig(path=":memory:"), RegistrySubclass())
    assert bad_registry.value.details["field"] == "registry"

    with pytest.raises(GrafxConfigurationError) as bad_ownership:
        assemble_database(
            DatabaseConfig(path=":memory:"),
            complete_registry,
            owns_ports=1,  # type: ignore[arg-type]
        )
    assert bad_ownership.value.details["field"] == "owns_ports"


def test_a_huge_finite_section_timeout_is_supported_without_numeric_overflow() -> None:
    database = bootstrap.open_database(
        DatabaseConfig(path=":memory:", commit_lock_timeout_seconds=1e308)
    )
    database.close()


def test_custom_registry_does_not_disable_the_process_global_checksum_selector(
    complete_registry: PortRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: list[str] = []

    def install(config: DatabaseConfig) -> str:
        observed.append(config.checksum)
        return "pure"

    monkeypatch.setattr(bootstrap, "install_checksum", install)
    database = bootstrap.open_database(
        DatabaseConfig(path=":memory:", checksum="pure"), registry=complete_registry
    )
    database.close()
    assert observed == ["pure"]


def test_open_database_with_a_complete_registry_assembles_on_the_caller_ports(
    complete_registry: PortRegistry,
) -> None:
    # This was a seam test: every port bound satisfied the fail-closed gate, and the call then
    # ran into a typed "not built yet". C11 delivered the assembly, so it completes -- and it
    # completes on exactly the objects the caller handed over. Identity, not type: a composition
    # root that rebuilt an equivalent adapter would pass a type check and still be wrong.
    database = bootstrap.open_database(
        DatabaseConfig(path=":memory:"), registry=complete_registry
    )
    try:
        for slot in PortRegistry.REQUIRED:
            # This is a composition-root identity test. Public properties intentionally return
            # immutable observations now; the private slot is where assembly must retain the
            # exact caller-owned collaborator.
            held = getattr(database, f"_{slot}")
            if slot == "metrics":
                # The one slot that is deliberately NOT the caller's object: a host-supplied
                # sink may do anything at all, and a raise on the post-commit gauge made a
                # durable commit report failure -- so the engine sees a containment shell, and
                # the caller's sink is what it contains.
                assert type(held).__name__ == "ContainedMetricsSink", slot
                assert held.inner is complete_registry.get(slot), slot
                continue
            assert held is complete_registry.get(slot), slot
    finally:
        database.close()


def test_the_default_adapter_table_only_names_known_slots() -> None:
    # Flagged in round 7 as a forward guard: the table was empty, so the loop body never ran and
    # the assertion held by having nothing to check. C11 filled it, so the loop finally executes
    # -- and the set equality below is what makes that non-vacuous, because a loop over an empty
    # mapping passes this test no matter what the body says.
    table = bootstrap._DEFAULT_PORT_FACTORIES
    assert set(table) == set(PortRegistry.REQUIRED), sorted(table)
    for slot, factory in table.items():
        assert slot in PortRegistry.REQUIRED, slot
        assert callable(factory), slot


def test_the_default_adapter_table_cannot_be_mutated_by_a_caller() -> None:
    with pytest.raises(TypeError):
        bootstrap._DEFAULT_PORT_FACTORIES["storage"] = lambda config: None  # type: ignore[index]


def test_the_bootstrap_keeps_the_caller_registry(
    complete_registry: PortRegistry,
) -> None:
    # A caller-supplied composition is used as given; the bootstrap never silently replaces a
    # bound adapter with a default one. Now that the open succeeds, the substitution this guards
    # against would be invisible without checking the registry after the call, not just before.
    storage = complete_registry.get("storage")
    database = bootstrap.open_database(
        DatabaseConfig(path=":memory:"), registry=complete_registry
    )
    try:
        assert complete_registry.get("storage") is storage
        assert database._storage is storage
        assert database.storage is not storage
    finally:
        database.close()


# --- the factory shape amendment A13 requires --------------------------------------------------


def test_the_build_order_covers_every_required_slot_exactly_once() -> None:
    assert set(BUILD_ORDER) == set(PortRegistry.REQUIRED)
    assert len(BUILD_ORDER) == len(set(BUILD_ORDER)) == len(PortRegistry.REQUIRED)


def test_the_build_order_puts_dependencies_first() -> None:
    # A13: the coordinator is built from the storage device and the clock, and reports lease
    # waits through the metrics sink, so all three precede it.
    position = {slot: index for index, slot in enumerate(BUILD_ORDER)}
    for dependency in ("storage", "clock", "metrics"):
        assert position[dependency] < position["coordinator"], dependency


def test_the_lock_directory_is_the_control_subdirectory_of_the_database() -> None:
    directory = lock_directory(DatabaseConfig(path="./mydb"))
    assert directory is not None
    assert Path(directory) == Path("./mydb") / CONTROL_DIRECTORY_NAME
    assert Path(directory).name == "control"


def test_an_in_memory_database_has_no_lock_directory() -> None:
    # A13 selects the process-wide section mode with None, which is the whole truth for a device
    # that is never shared between processes.
    assert lock_directory(DatabaseConfig(path=":memory:")) is None


def test_the_coordinator_settings_are_the_mapping_the_amendment_names() -> None:
    config = DatabaseConfig(
        path="./mydb",
        lease_ttl_seconds=7.0,
        lease_timeout_seconds=11.0,
        reader_stall_threshold_seconds=21.0,
        commit_lock_timeout_seconds=33.0,
    )
    assert coordinator_settings(config) == {
        "ttl_seconds": 7.0,
        "owner_stall_threshold": 7.0,
        "reader_stall_threshold": 21.0,
        "section_timeout": 33.0,
    }


def test_every_coordinator_setting_is_a_parameter_c3_actually_accepts() -> None:
    # A44: identify by identity, not by name. The mapping is asserted against the real
    # constructor, so a rename in C3 fails here rather than at W4 wiring time.
    from okto_grafx.adapters.coordination_local import LocalProcessCoordinator

    parameters = inspect.signature(LocalProcessCoordinator.__init__).parameters
    settings = coordinator_settings(DatabaseConfig(path="./mydb"))
    for keyword in settings:
        assert keyword in parameters, (
            f"coordinator_settings passes {keyword!r}, which "
            f"LocalProcessCoordinator.__init__ does not accept"
        )
        assert parameters[keyword].kind is inspect.Parameter.KEYWORD_ONLY, keyword


def test_the_coordinator_accepts_the_whole_mapping_at_once() -> None:
    # Binding the arguments proves the mapping is usable as a unit, which is what C11 will do.
    from okto_grafx.adapters.coordination_local import LocalProcessCoordinator

    signature = inspect.signature(LocalProcessCoordinator.__init__)
    settings = coordinator_settings(DatabaseConfig(path="./mydb"))
    bound = signature.bind_partial(None, storage=None, clock=None, **settings)
    assert set(settings) <= set(bound.arguments)


def test_the_owner_stall_threshold_follows_the_lease_not_the_reader() -> None:
    # The one mapping worth an assertion of its own: an owner that has not renewed within its
    # own lease is the definition of a stalled owner, so it must not inherit the reader value.
    config = DatabaseConfig(
        path="./mydb", lease_ttl_seconds=4.0, reader_stall_threshold_seconds=40.0
    )
    settings = coordinator_settings(config)
    assert settings["owner_stall_threshold"] == 4.0
    assert settings["owner_stall_threshold"] != settings["reader_stall_threshold"]


def test_a_context_hands_back_a_port_that_is_already_built(
    fake_ports: dict[str, object],
) -> None:
    context = PortContext(
        config=DatabaseConfig(path="./mydb"),
        ports={"storage": fake_ports["storage"], "clock": fake_ports["clock"]},
        lock_directory="./mydb/control",
    )
    assert context.require("storage") is fake_ports["storage"]
    assert context.require("clock") is fake_ports["clock"]


def test_a_context_refuses_a_dependency_that_is_not_built_yet(
    fake_ports: dict[str, object],
) -> None:
    context = PortContext(
        config=DatabaseConfig(path="./mydb"),
        ports={"storage": fake_ports["storage"]},
        lock_directory=None,
    )
    with pytest.raises(GrafxPortNotConfigured) as raised:
        context.require("coordinator")
    assert raised.value.details["missing"] == ["coordinator"]
    assert raised.value.details["available"] == ["storage"]


def test_a_factory_receives_the_ports_built_before_it(
    monkeypatch: pytest.MonkeyPatch, fake_ports: dict[str, object]
) -> None:
    # The behaviour A13 asks for, proved with a synthetic adapter table: dependency order, and
    # every earlier instance visible to every later factory.
    seen: dict[str, tuple[str, ...]] = {}
    call_order: list[str] = []
    directories: dict[str, str | None] = {}

    def make(slot: str) -> object:
        def factory(context: PortContext) -> object:
            call_order.append(slot)
            seen[slot] = tuple(context.ports)
            directories[slot] = context.lock_directory
            return fake_ports[slot]

        return factory

    table = MappingProxyType({slot: make(slot) for slot in PortRegistry.REQUIRED})
    monkeypatch.setattr(bootstrap, "_DEFAULT_PORT_FACTORIES", table)

    registry = bootstrap.build_default_registry(DatabaseConfig(path="./mydb"))

    assert call_order == list(BUILD_ORDER)
    assert seen["storage"] == ()
    assert "storage" in seen["coordinator"] and "clock" in seen["coordinator"]
    assert seen["coordinator"] == tuple(BUILD_ORDER[: BUILD_ORDER.index("coordinator")])
    assert {Path(directory or "") for directory in directories.values()} == {
        Path("./mydb") / CONTROL_DIRECTORY_NAME
    }
    registry.require_complete()
    for slot in PortRegistry.REQUIRED:
        assert registry.get(slot) is fake_ports[slot]


def test_a_factory_for_an_in_memory_database_is_told_there_is_no_lock_directory(
    monkeypatch: pytest.MonkeyPatch, fake_ports: dict[str, object]
) -> None:
    directories: list[str | None] = []

    def factory(context: PortContext) -> object:
        directories.append(context.lock_directory)
        return fake_ports["storage"]

    monkeypatch.setattr(
        bootstrap, "_DEFAULT_PORT_FACTORIES", MappingProxyType({"storage": factory})
    )
    with pytest.raises(GrafxPortNotConfigured):
        bootstrap.build_default_registry(DatabaseConfig(path=":memory:"))
    assert directories == [None]


def test_a_factory_that_returns_a_wrong_shape_is_refused_at_bind_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        bootstrap,
        "_DEFAULT_PORT_FACTORIES",
        MappingProxyType({"storage": lambda context: object()}),
    )
    with pytest.raises(GrafxConfigurationError) as raised:
        bootstrap.build_default_registry(DatabaseConfig(path="./mydb"))
    assert raised.value.details["slot"] == "storage"


def test_the_lock_directory_is_named_for_the_parameter_it_feeds() -> None:
    # The seam C11 must wire: the coordinator adapter has a control_directory= parameter that is
    # a prefix INSIDE the storage namespace, defaulting to "control". This helper returns a HOST
    # path and belongs in lock_directory=. The names are one character apart in meaning, so the
    # helper is named for the parameter it feeds rather than for the directory it points at.
    assert bootstrap.lock_directory.__name__ == "lock_directory"
    assert not hasattr(bootstrap, "control_directory")
    assert CONTROL_DIRECTORY_NAME == "control"
    host_path = bootstrap.lock_directory(DatabaseConfig(path="./mydb"))
    assert host_path is not None
    assert Path(host_path).name == CONTROL_DIRECTORY_NAME
    assert str(Path(host_path).parent) != ""
    # The collision is called out where a wiring author will read it.
    assert "control_directory" in (bootstrap.lock_directory.__doc__ or "")


def test_the_openmetrics_default_is_applied_where_the_composition_happens() -> None:
    # A8 declares the default; the configuration stores None because None means "the caller did
    # not choose". Resolving it is a composition decision, so the constant is read here rather
    # than sitting unused.
    assert bootstrap.metrics_destination(DatabaseConfig(path="./mydb")) is None
    assert (
        bootstrap.metrics_destination(
            DatabaseConfig(path="./mydb", metrics="openmetrics")
        )
        == DEFAULT_OPENMETRICS_DESTINATION
    )
    assert (
        bootstrap.metrics_destination(
            DatabaseConfig(
                path="./mydb",
                metrics="openmetrics",
                metrics_destination="0.0.0.0:9100",
                allow_remote_metrics=True,
            )
        )
        == "0.0.0.0:9100"
    )
    assert (
        bootstrap.metrics_destination(
            DatabaseConfig(
                path="./mydb", metrics="json", metrics_destination="./m.json"
            )
        )
        == "./m.json"
    )


def test_the_json_sink_never_reaches_the_composition_root_without_a_destination() -> (
    None
):
    # The configuration refuses it first, so the resolver never has to invent one.
    with pytest.raises(GrafxConfigurationError):
        DatabaseConfig(path="./mydb", metrics="json")


# --- the CRC-32C implementation (D5 item 2, D2's native acceleration behind a port) ------------


@pytest.fixture(autouse=True)
def _restore_checksum() -> object:
    """Put back whatever CRC-32C implementation was in effect before a test changed it.

    The implementation is process-global on purpose: every component of a database must compute
    the same checksum, so it is installed once and not injected per object. That makes it shared
    state between tests, and a machine WITH a native provider is the only one where a test that
    left `native` behind can make the next test read something other than what it installed.
    """
    from okto_grafx.adapters.checksum_native import NativeCrc32c
    from okto_grafx.adapters.checksum_pure import PureCrc32c
    from okto_grafx.domain.page.checksum import crc32c_implementation

    before = crc32c_implementation()
    yield
    if crc32c_implementation() != before:
        (NativeCrc32c() if before == "native" else PureCrc32c()).install()


def test_the_pure_selector_installs_the_reference() -> None:
    """The reference is always installable: it is the thing every candidate is measured against."""
    assert (
        bootstrap.install_checksum(DatabaseConfig(path=":memory:", checksum="pure"))
        == "pure"
    )
    assert crc32c_implementation() == "pure"


def test_auto_never_refuses_and_leaves_a_working_implementation() -> None:
    """ "auto" asks for the fastest CORRECT answer, and the reference is a correct answer.

    A composition root that refused to open a database because an optional accelerator was absent
    would make an optional extra mandatory.
    """
    name = bootstrap.install_checksum(DatabaseConfig(path=":memory:", checksum="auto"))
    assert name in {"pure", "native"}
    assert crc32c(b"okto") == crc32c_reference(b"okto")


def test_the_native_selector_refuses_rather_than_quietly_using_the_reference() -> None:
    """A caller who asked for the accelerator and silently got the reference measures a lie."""
    config = DatabaseConfig(path=":memory:", checksum="native")
    try:
        name = bootstrap.install_checksum(config)
    except GrafxConfigurationError as refusal:
        assert refusal.details["field"] == "checksum"
        assert "accel" in refusal.message
    else:
        assert name == "native"
    finally:
        bootstrap.install_checksum(DatabaseConfig(path=":memory:", checksum="pure"))


@pytest.mark.parametrize("selector", ["native", "auto"])
def test_a_typed_native_provider_failure_keeps_its_identity(
    selector: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from okto_grafx.adapters import checksum_native

    marker = GrafxDeviceFull("provider storage refusal")

    def refuse() -> object:
        raise marker

    monkeypatch.setattr(checksum_native, "NativeCrc32c", refuse)
    with pytest.raises(GrafxDeviceFull) as raised:
        bootstrap.install_checksum(DatabaseConfig(path=":memory:", checksum=selector))
    assert raised.value is marker


def test_an_unknown_checksum_selector_is_refused_by_the_configuration() -> None:
    with pytest.raises(GrafxConfigurationError) as refusal:
        DatabaseConfig(path=":memory:", checksum="fastest")
    assert refusal.value.details["field"] == "checksum"


def test_whatever_is_installed_agrees_with_the_reference_byte_for_byte() -> None:
    """The property that makes accelerating a checksum safe at all.

    ``install_crc32c`` replays the acceptance corpus before installing, so this cannot fail while
    that guard stands -- which is the point: the file format does not depend on which machine
    wrote it, and that is enforced at the door rather than by remembering to run a test.
    """
    bootstrap.install_checksum(DatabaseConfig(path=":memory:", checksum="auto"))
    corpus = [b"", b"\x00", b"okto grafx", bytes(range(256)), b"\xff" * 4096]
    for payload in corpus:
        assert crc32c(payload) == crc32c_reference(payload)
        # Chained: the seed is where a provider written for a different argument order goes wrong.
        assert crc32c(payload, crc32c(b"seed")) == crc32c_reference(
            payload, crc32c_reference(b"seed")
        )
