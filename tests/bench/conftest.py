"""Make the repository root importable, so the bench tests can import ``bench``.

``bench/`` is not part of the distribution (it ships in no wheel, SPEC-M1 TR-9), so it is not on
``sys.path`` through the package the way ``okto_grafx`` is: ``[tool.pytest.ini_options] pythonpath``
adds ``src`` only. Under pytest's default import mode the directory inserted for a test module is
its own basedir, which is ``tests/bench``, not the root. Inserting the root here is what lets
``import bench.coverage`` resolve to the tree under review rather than to anything installed.

Deliberately narrow: this conftest adds a path and nothing else. Every fixture the bench tests use
is defined in the test module that uses it, so nothing here can change what another component's
tests see.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
"""The repository root, which holds both ``bench`` and ``pyproject.toml``."""

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
