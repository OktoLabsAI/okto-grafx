"""Adapters: the only place in Okto Grafx where mechanism lives.

Files, locks, clocks, sockets, third-party numerics and platform differences are implemented
here and nowhere else. An adapter may import the domain; the domain may never import an
adapter, and ``tests/test_import_boundary.py`` enforces that direction with budget zero.
"""

from __future__ import annotations

__all__: list[str] = []
