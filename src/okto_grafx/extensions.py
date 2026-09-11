"""Public trusted extension SPI; code is supplied by the host, never loaded from a store."""

from __future__ import annotations

from okto_grafx.domain.query.extensions import ExtensionRegistry, ScalarFunction, TabularProcedure

__all__ = ["ExtensionRegistry", "ScalarFunction", "TabularProcedure"]
