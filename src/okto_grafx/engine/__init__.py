"""Engine: the orchestration layer, pure by the same rule as the domain.

Engine classes receive every port through their constructor and hold no module level state, so
one database never observes the memory pressure or the lease of another (FR-13, BR-8).
"""

from __future__ import annotations

__all__: list[str] = []
