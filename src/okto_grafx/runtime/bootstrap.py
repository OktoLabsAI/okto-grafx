"""The composition root (CONTRACT.md section 5).

This is the only place allowed to know both the pure core and the concrete adapters. It builds
the default adapters for a configuration, verifies that every port slot is filled, and only
then assembles the engine. Nothing above this module ever imports an adapter.

The adapter table below is empty in this build: the foundation (C0) delivers the ports and the
fail-closed rules, and each later wave fills its own slot by adding one import and one entry.
Until then, opening a database without an explicit registry fails closed with
GrafxPortNotConfigured, which is exactly the behaviour required by BR-8 and G5.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import TYPE_CHECKING

from okto_grafx.domain.errors import GrafxUnsupportedOperation
from okto_grafx.runtime.config import DatabaseConfig
from okto_grafx.runtime.registry import PortRegistry

if TYPE_CHECKING:
    # The engine level database is delivered by C11 in wave W4.
    from okto_grafx.engine.database import Database

__all__ = ["PortFactory", "build_default_registry", "open_database"]

PortFactory = Callable[[DatabaseConfig], object]
"""How a slot is built: one configuration in, one adapter instance out."""

_DEFAULT_PORT_FACTORIES: Mapping[str, PortFactory] = MappingProxyType({})
"""Slot to adapter factory.

Wave W1 adds "codec" (C1), "storage" (C2), "clock" and "coordinator" (C3), "metrics" and
"events" (C8); wave W3 adds "vector_math" (C9). Every entry is a one-line addition here plus
its import; no other module changes.
"""


def build_default_registry(config: DatabaseConfig) -> PortRegistry:
    """Build the default adapters for this configuration and return a complete registry.

    Raises GrafxPortNotConfigured, listing every slot that stayed empty, when the current build
    cannot provide a default for all of them.
    """
    registry = PortRegistry()
    for slot, factory in _DEFAULT_PORT_FACTORIES.items():
        registry.bind(slot, factory(config))
    registry.require_complete()
    return registry


def open_database(config: DatabaseConfig, *, registry: PortRegistry | None = None) -> Database:
    """Open a database against the given configuration.

    With no registry the default adapters are built for the configuration; with a registry the
    caller owns the composition. Either way every required port slot is verified before the
    engine is assembled, so an incomplete composition refuses to start.
    """
    ports = build_default_registry(config) if registry is None else registry
    ports.require_complete()
    return _assemble_database(config, ports)


def _assemble_database(config: DatabaseConfig, ports: PortRegistry) -> Database:
    """Wire the engine on top of a complete port registry."""
    raise GrafxUnsupportedOperation(
        "Assembling a database needs the storage, transaction and query components, which are "
        "not part of this build; the foundation provides the ports and the configuration only.",
        path=config.path,
        storage=type(ports.get("storage")).__name__,
    )
