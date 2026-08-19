"""Pure domain of Okto Grafx.

Nothing in this package touches a mechanism: no file system, no clock, no process, no thread
and no third-party library. Everything the domain needs from the outside world arrives through
the ports declared in :mod:`okto_grafx.domain.ports`. The rule is enforced with budget zero by
``tests/test_import_boundary.py`` (SPEC-M1 TR-1, CONTRACT.md G2).
"""

from __future__ import annotations

__all__: list[str] = []
