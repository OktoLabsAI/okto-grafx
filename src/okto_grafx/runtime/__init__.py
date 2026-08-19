"""The composition root of Okto Grafx.

Configuration, the fail-closed port registry and the bootstrap that binds adapters to ports
live here. This is the only layer that is allowed to know both the pure core and the concrete
adapters (CONTRACT.md section 5).
"""

from __future__ import annotations

from okto_grafx.runtime.config import DatabaseConfig
from okto_grafx.runtime.registry import PortRegistry

__all__ = ["DatabaseConfig", "PortRegistry"]
