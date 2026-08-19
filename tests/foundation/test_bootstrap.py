"""The composition root refuses to start on an incomplete composition (BR-8, guideline G5)."""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import (
    GrafxPortNotConfigured,
    GrafxUnsupportedOperation,
)
from okto_grafx.runtime import bootstrap
from okto_grafx.runtime.config import DatabaseConfig
from okto_grafx.runtime.registry import PortRegistry


def test_building_the_default_registry_fails_closed_on_every_unfilled_slot() -> None:
    # This build ships the ports, not the adapters: every slot is still missing and the error
    # must name all of them at once instead of one per attempt.
    with pytest.raises(GrafxPortNotConfigured) as raised:
        bootstrap.build_default_registry(DatabaseConfig(path=":memory:"))
    assert raised.value.details["missing"] == list(PortRegistry.REQUIRED)


def test_open_database_without_a_registry_fails_closed() -> None:
    with pytest.raises(GrafxPortNotConfigured) as raised:
        bootstrap.open_database(DatabaseConfig(path=":memory:"))
    assert raised.value.details["missing"] == list(PortRegistry.REQUIRED)


def test_open_database_with_an_incomplete_registry_lists_the_gaps(
    fake_ports: dict[str, object]
) -> None:
    registry = PortRegistry()
    registry.bind("storage", fake_ports["storage"])
    registry.bind("clock", fake_ports["clock"])
    registry.bind("coordinator", fake_ports["coordinator"])

    with pytest.raises(GrafxPortNotConfigured) as raised:
        bootstrap.open_database(DatabaseConfig(path=":memory:"), registry=registry)

    assert raised.value.details["missing"] == ["codec", "metrics", "vector_math", "events"]


def test_open_database_with_a_complete_registry_reaches_the_assembly_seam(
    complete_registry: PortRegistry,
) -> None:
    # With every port bound the fail-closed gate is satisfied, so the call proceeds to the
    # engine assembly that later waves deliver. The failure is typed and explicit.
    with pytest.raises(GrafxUnsupportedOperation) as raised:
        bootstrap.open_database(DatabaseConfig(path=":memory:"), registry=complete_registry)
    assert raised.value.code == "unsupported_operation"
    assert raised.value.details["path"] == ":memory:"
    assert raised.value.details["storage"] == "FakeStorageDevice"


def test_the_default_adapter_table_only_names_known_slots() -> None:
    # Later waves fill this table; nothing may appear in it that the registry does not accept.
    for slot in bootstrap._DEFAULT_PORT_FACTORIES:
        assert slot in PortRegistry.REQUIRED


def test_the_default_adapter_table_cannot_be_mutated_by_a_caller() -> None:
    with pytest.raises(TypeError):
        bootstrap._DEFAULT_PORT_FACTORIES["storage"] = lambda config: None  # type: ignore[index]


def test_the_bootstrap_keeps_the_caller_registry(complete_registry: PortRegistry) -> None:
    # A caller-supplied composition is used as given; the bootstrap never silently replaces a
    # bound adapter with a default one.
    storage = complete_registry.get("storage")
    with pytest.raises(GrafxUnsupportedOperation):
        bootstrap.open_database(DatabaseConfig(path=":memory:"), registry=complete_registry)
    assert complete_registry.get("storage") is storage
