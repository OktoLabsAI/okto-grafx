"""The hexagonal boundary gate (CONTRACT.md G2, G2b, A5, A12; SPEC-M1 TR-1, SPEC-VEC TR-1).

Budget: ZERO. Every Python file under ``domain/`` and ``engine/`` is parsed and rejected when it
reaches outside the pure core. The rules, and why each one exists:

* **Imports** must name a module in the explicit standard library allowlist, or a module of the
  pure core. ``from X import Y`` is checked twice, for ``X`` and for the dotted path ``X.Y``,
  because ``from okto_grafx import adapters`` imports the adapters layer just as surely as
  ``from okto_grafx.adapters import storage_local`` does.
* **The package is imported with ``from X import Y`` only.** A statement import binds the name
  ``okto_grafx`` in the module, and once any other module has imported the composition root,
  ``okto_grafx.runtime.config`` is reachable through that binding without a single forbidden
  import appearing in the file.
* **The dependency arrow points one way**: the domain may not import the engine (A12), and
  neither may import adapters, runtime, api or cli.
* **Builtins that open files or execute text** (``open``, ``eval``, ``exec``, ``compile``,
  ``__import__``) are refused wherever the name is *loaded*, not only where it is called, since
  ``_reader = open`` defeats a call-site-only rule. The introspection roots ``__builtins__``,
  ``globals``, ``locals`` and ``vars`` are refused for the same reason: they are the other door
  to the same builtins. A pure module has no legitimate reason to mention any of these names, so
  the false-positive risk (a parameter that happens to be called ``open``) is accepted knowingly.
* **``TYPE_ONLY_MODULES`` may appear only under a trustworthy ``if TYPE_CHECKING:`` guard** --
  one whose name really is ``typing.TYPE_CHECKING`` and is never reassigned in the module.
  Writing ``TYPE_CHECKING = True`` would otherwise turn the escape hatch into an open door.
* **The one runtime observation exception is exact**: only
  ``okto_grafx.engine.txn_manager <- time.perf_counter_ns`` is accepted, unaliased and alone in
  its import. It times diagnostics without invoking a host-supplied Clock while commit locks are
  held; it never participates in a storage, lease or visibility decision.

The gate also proves it can fail, with synthetic sources for every rule, so a silent regression
of the checker itself is a failure too.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
PACKAGE_ROOT: Path = PROJECT_ROOT / "src" / "okto_grafx"
PURE_PACKAGES: tuple[str, ...] = ("domain", "engine")
PACKAGE_NAME: str = "okto_grafx"

ALLOWED_STDLIB_MODULES: frozenset[str] = frozenset(
    {
        "__future__",
        "abc",
        "array",
        "bisect",
        "collections",
        "collections.abc",
        "contextlib",
        "dataclasses",
        "enum",
        "functools",
        "hashlib",
        "itertools",
        "math",
        "struct",
        "types",
        "typing",
        "zlib",
    }
)
"""The only modules the pure core may import. Anything else is mechanism or a dependency.

``bisect`` is an algorithm over a list the caller already holds -- no clock, no randomness,
no device, no platform -- and the WALs LSN index rests on it (D5 item 1); it is pure in
exactly the sense ``math`` and ``itertools`` are.

``types.MappingProxyType`` gives derived domain lookup tables an immutable, O(1) view without
introducing I/O, time, randomness or a dependency on a mechanism layer.  The module is therefore
pure under the same criterion as ``dataclasses`` and ``collections``.

``zlib`` is the deterministic algorithm that defines the durable WRITE_PAGE v2 grammar. It has
no I/O, time, randomness or platform decision, and keeping bounded inflate beside that grammar
prevents an adapter from acquiring authority over WAL interpretation.

``uuid`` is deliberately absent: ``uuid4`` is unseeded randomness and ``uuid1`` reads the wall
clock, both of which G2b and amendment A5 keep out of the domain. The identifier type is still
reachable through ``TYPE_ONLY_MODULES``.

``time`` is also deliberately absent. Its sole diagnostic observation is enforced separately by
the exact consumer/origin/symbol triple in ``EXACT_RUNTIME_OBSERVATION_IMPORTS``.
"""

TYPE_ONLY_MODULES: frozenset[str] = frozenset({"uuid"})
"""Modules the pure core may name for typing only, under a trustworthy TYPE_CHECKING guard."""

EXACT_RUNTIME_OBSERVATION_IMPORTS: frozenset[tuple[str, str, str]] = frozenset(
    {
        (
            "okto_grafx.engine.txn_manager",
            "time",
            "perf_counter_ns",
        ),
    }
)
"""Single-symbol diagnostic observations that are safe from host callback re-entry.

This is deliberately keyed by consumer, origin and exact unaliased symbol. It does not make
``time`` an allowed pure-core module and cannot be widened to another file or clock operation by
adding an import statement alone.
"""

FORBIDDEN_FOR_EVERY_PURE_MODULE: tuple[str, ...] = (
    "okto_grafx.adapters",
    "okto_grafx.runtime",
    "okto_grafx.api",
    "okto_grafx.cli",
)
"""Layers no pure module may import; the dependency arrow points the other way."""

FORBIDDEN_FOR_DOMAIN: tuple[str, ...] = ("okto_grafx.engine",)
"""Amendment A12: the engine orchestrates the domain, so the domain never reaches back."""

FORBIDDEN_ATTRIBUTES: frozenset[tuple[str, str]] = frozenset(
    {("sys", "platform"), ("os", "name")}
)
"""Platform sniffing. A platform difference belongs to an adapter, never to the core."""

FORBIDDEN_BUILTIN_NAMES: frozenset[str] = frozenset(
    {"open", "eval", "exec", "compile", "__import__"}
)
"""Builtins that touch the file system or execute code built at runtime."""

INTROSPECTION_ROOTS: frozenset[str] = frozenset(
    {"__builtins__", "globals", "locals", "vars"}
)
"""Names that hand back a namespace, and with it every builtin the rule above refuses."""

TYPE_CHECKING_ATTRIBUTE: str = "TYPE_CHECKING"


@dataclass
class _GuardAnalysis:
    """What the module says about TYPE_CHECKING, and what it does to it."""

    guard_names: set[str] = field(default_factory=set)
    typing_modules: set[str] = field(default_factory=set)
    violations: list[str] = field(default_factory=list)


def _module_name(path: Path) -> str:
    """Return the dotted module name of a file inside the package tree.

    A package initialiser keeps its ``__init__`` tail, which is what makes relative import
    resolution uniform: level 1 always drops exactly one name.
    """
    relative = path.relative_to(PACKAGE_ROOT.parent).with_suffix("")
    return ".".join(relative.parts)


def _forbidden_packages(module_name: str) -> tuple[str, ...]:
    """Return the internal packages this module may not import, by the layer it lives in."""
    if module_name.startswith(f"{PACKAGE_NAME}.domain"):
        return FORBIDDEN_FOR_EVERY_PURE_MODULE + FORBIDDEN_FOR_DOMAIN
    return FORBIDDEN_FOR_EVERY_PURE_MODULE


def _resolve_relative(module_name: str, level: int, module: str | None) -> str:
    """Resolve a relative import into the absolute module name it refers to."""
    parts = module_name.split(".")
    if level > len(parts):
        return module or ""
    # Level 1 is the package that holds the module; each extra level climbs one package.
    base = parts[: len(parts) - level]
    if module:
        base = [*base, *module.split(".")]
    return ".".join(base)


def _is_allowed_import(
    imported: str, *, type_checking: bool, forbidden: tuple[str, ...]
) -> bool:
    """Return True when the imported dotted path is allowed inside this pure module."""
    if not imported:
        return False
    if imported in ALLOWED_STDLIB_MODULES:
        return True
    root = imported.split(".", 1)[0]
    if root in ALLOWED_STDLIB_MODULES:
        return True
    if type_checking and root in TYPE_ONLY_MODULES:
        return True
    if imported == PACKAGE_NAME or imported.startswith(f"{PACKAGE_NAME}."):
        return not any(
            imported == layer or imported.startswith(f"{layer}.") for layer in forbidden
        )
    return False


def _rebound_names(tree: ast.AST, trusted_imports: set[int]) -> set[str]:
    """Return every name the module binds by assignment, deletion or a non-typing import alias.

    An alias from ``typing`` itself is not a rebinding: ``from typing import TYPE_CHECKING as
    _TC`` still names the real flag. An alias from anywhere else is, which is exactly how a
    module would try to smuggle its own flag in under a trusted name.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            if id(node) in trusted_imports:
                continue
            for alias in node.names:
                if alias.asname:
                    bound.add(alias.asname)
    return bound


def _guard_analysis(tree: ast.AST, module_name: str) -> _GuardAnalysis:
    """Work out which names may be trusted as a TYPE_CHECKING guard in this module."""
    analysis = _GuardAnalysis()
    imported_guards: set[str] = set()
    trusted_imports: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "typing" and not node.level:
            trusted_imports.add(id(node))
            for alias in node.names:
                if alias.name == TYPE_CHECKING_ATTRIBUTE:
                    imported_guards.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "typing":
                    trusted_imports.add(id(node))
                    analysis.typing_modules.add(alias.asname or "typing")

    assigned = _rebound_names(tree, trusted_imports)
    for name in sorted(imported_guards | {TYPE_CHECKING_ATTRIBUTE}):
        if name in assigned:
            analysis.violations.append(
                f"{module_name}: assigns to {name!r}, which a TYPE_CHECKING guard must not be"
            )
    analysis.guard_names = {name for name in imported_guards if name not in assigned}
    analysis.typing_modules = {
        name for name in analysis.typing_modules if name not in assigned
    }
    return analysis


def _is_trusted_guard(test: ast.expr, analysis: _GuardAnalysis) -> bool:
    """Return True for a TYPE_CHECKING guard that really is the one from typing."""
    if isinstance(test, ast.Name):
        return test.id in analysis.guard_names
    if isinstance(test, ast.Attribute) and test.attr == TYPE_CHECKING_ATTRIBUTE:
        return isinstance(test.value, ast.Name) and test.value.id in analysis.typing_modules
    return False


def _guarded_import_nodes(tree: ast.AST, analysis: _GuardAnalysis) -> set[int]:
    """Return the identities of the import nodes that live under a trustworthy guard."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not _is_trusted_guard(node.test, analysis):
            continue
        for statement in node.body:
            for descendant in ast.walk(statement):
                if isinstance(descendant, (ast.Import, ast.ImportFrom)):
                    guarded.add(id(descendant))
    return guarded


def scan_source(module_name: str, source: str) -> list[str]:
    """Return one message per boundary violation found in the source of a pure-core module."""
    violations: list[str] = []
    tree = ast.parse(source, filename=f"{module_name}.py")
    forbidden = _forbidden_packages(module_name)
    analysis = _guard_analysis(tree, module_name)
    violations.extend(analysis.violations)
    guarded = _guarded_import_nodes(tree, analysis)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            type_checking = id(node) in guarded
            for alias in node.names:
                if alias.name == PACKAGE_NAME or alias.name.startswith(f"{PACKAGE_NAME}."):
                    violations.append(
                        f"{module_name}:{node.lineno} imports {alias.name!r} as a statement; "
                        f"the pure core uses 'from ... import ...' so the package root is never bound"
                    )
                    continue
                if not _is_allowed_import(
                    alias.name, type_checking=type_checking, forbidden=forbidden
                ):
                    violations.append(f"{module_name}:{node.lineno} imports {alias.name!r}")
        elif isinstance(node, ast.ImportFrom):
            type_checking = id(node) in guarded
            imported = (
                _resolve_relative(module_name, node.level, node.module)
                if node.level
                else (node.module or "")
            )
            exact_observation = (
                len(node.names) == 1
                and node.names[0].asname is None
                and (module_name, imported, node.names[0].name)
                in EXACT_RUNTIME_OBSERVATION_IMPORTS
            )
            if exact_observation:
                continue
            if not _is_allowed_import(
                imported, type_checking=type_checking, forbidden=forbidden
            ):
                violations.append(f"{module_name}:{node.lineno} imports from {imported!r}")
                continue
            before = len(violations)
            for alias in node.names:
                if alias.name == "*":
                    if imported == PACKAGE_NAME:
                        violations.append(
                            f"{module_name}:{node.lineno} imports * from the package root"
                        )
                    continue
                candidate = f"{imported}.{alias.name}"
                if not _is_allowed_import(
                    candidate, type_checking=type_checking, forbidden=forbidden
                ):
                    violations.append(f"{module_name}:{node.lineno} imports {candidate!r}")
            if imported == PACKAGE_NAME and len(violations) == before:
                # Importing anything FROM the package root executes okto_grafx/__init__.py,
                # which C11 extends with connect and Database (section 10, A10). Reported only
                # when the name is not already a forbidden layer, whose message says more.
                violations.append(
                    f"{module_name}:{node.lineno} imports from the package root; "
                    f"import the concrete module instead"
                )
        elif isinstance(node, ast.Attribute):
            value = node.value
            if isinstance(value, ast.Name) and (value.id, node.attr) in FORBIDDEN_ATTRIBUTES:
                violations.append(f"{module_name}:{node.lineno} reads {value.id}.{node.attr}")
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in FORBIDDEN_BUILTIN_NAMES:
                violations.append(f"{module_name}:{node.lineno} names the builtin {node.id!r}")
            elif node.id in INTROSPECTION_ROOTS:
                violations.append(
                    f"{module_name}:{node.lineno} reaches the namespace through {node.id!r}"
                )

    return violations


def _pure_core_files() -> list[Path]:
    """Return every Python file of the pure core, sorted for a stable report."""
    files: list[Path] = []
    for package in PURE_PACKAGES:
        files.extend(sorted((PACKAGE_ROOT / package).rglob("*.py")))
    return files


PURE_CORE_FILES: list[Path] = _pure_core_files()

__all__ = ["ALLOWED_STDLIB_MODULES", "TYPE_ONLY_MODULES", "scan_source"]


def test_the_gate_actually_sees_the_pure_core() -> None:
    # A gate that walks an empty tree passes for the wrong reason.
    assert PACKAGE_ROOT.is_dir()
    assert len(PURE_CORE_FILES) >= 10
    assert any(path.parts[-2] == "ports" for path in PURE_CORE_FILES)


def test_the_allowlist_excludes_the_modules_the_contract_forbids() -> None:
    # G2b and A5: randomness in the domain comes from domain.rand.SplitMix64, never from
    # random or from uuid4; time comes from the Clock port, never from time.
    for module in ("random", "uuid", "time", "os", "sys", "pathlib", "threading", "numpy"):
        assert module not in ALLOWED_STDLIB_MODULES


def test_the_domain_carries_the_extra_layer_rule() -> None:
    assert "okto_grafx.engine" in _forbidden_packages("okto_grafx.domain.ids")
    assert "okto_grafx.engine" not in _forbidden_packages("okto_grafx.engine.heap_store")
    for layer in FORBIDDEN_FOR_EVERY_PURE_MODULE:
        assert layer in _forbidden_packages("okto_grafx.domain.ids")
        assert layer in _forbidden_packages("okto_grafx.engine.heap_store")


@pytest.mark.parametrize(
    "path", PURE_CORE_FILES, ids=lambda path: str(path.relative_to(PACKAGE_ROOT)).replace("\\", "/")
)
def test_pure_core_module_has_no_boundary_violation(path: Path) -> None:
    violations = scan_source(_module_name(path), path.read_text(encoding="utf-8"))
    assert violations == []


def test_the_budget_over_the_whole_pure_core_is_zero() -> None:
    violations: list[str] = []
    for path in PURE_CORE_FILES:
        violations.extend(scan_source(_module_name(path), path.read_text(encoding="utf-8")))
    assert violations == [], "\n".join(violations)


def test_every_pure_core_module_postpones_its_annotations() -> None:
    # CONTRACT.md section 11 item 3, and the reason forward references to later waves cost
    # nothing at import time.
    for path in PURE_CORE_FILES:
        source = path.read_text(encoding="utf-8")
        if not source.strip():
            continue
        assert "from __future__ import annotations" in source, path


# --- the checker must be able to fail ---------------------------------------------------------

DOMAIN_MODULE: str = "okto_grafx.domain.probe"
NESTED_MODULE: str = "okto_grafx.domain.ports.storage"
ENGINE_MODULE: str = "okto_grafx.engine.probe"
TXN_MANAGER_MODULE: str = "okto_grafx.engine.txn_manager"

VIOLATING_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("import os", DOMAIN_MODULE, "import os\n"),
    ("import os.path", DOMAIN_MODULE, "import os.path\n"),
    ("from os import fsync", DOMAIN_MODULE, "from os import fsync\n"),
    ("import sys", DOMAIN_MODULE, "import sys\n"),
    ("import threading", DOMAIN_MODULE, "import threading\n"),
    ("import time", DOMAIN_MODULE, "import time\n"),
    ("engine imports time", TXN_MANAGER_MODULE, "import time\n"),
    (
        "engine imports another monotonic timer",
        TXN_MANAGER_MODULE,
        "from time import monotonic_ns\n",
    ),
    (
        "engine imports wall time",
        TXN_MANAGER_MODULE,
        "from time import time\n",
    ),
    (
        "engine aliases the diagnostic timer",
        TXN_MANAGER_MODULE,
        "from time import perf_counter_ns as timer\n",
    ),
    (
        "another engine module imports the diagnostic timer",
        ENGINE_MODULE,
        "from time import perf_counter_ns\n",
    ),
    (
        "engine broadens the diagnostic timer import",
        TXN_MANAGER_MODULE,
        "from time import perf_counter_ns, monotonic\n",
    ),
    ("import mmap", DOMAIN_MODULE, "import mmap\n"),
    ("import socket", DOMAIN_MODULE, "import socket\n"),
    ("import pathlib", DOMAIN_MODULE, "from pathlib import Path\n"),
    ("import numpy", DOMAIN_MODULE, "import numpy as np\n"),
    ("import random", DOMAIN_MODULE, "import random\n"),
    ("import re", DOMAIN_MODULE, "import re\n"),
    ("import uuid at runtime", DOMAIN_MODULE, "import uuid\n"),
    ("from uuid at runtime", DOMAIN_MODULE, "from uuid import UUID, uuid4\n"),
    ("platform sniffing", DOMAIN_MODULE, "def f(sys):\n    return sys.platform\n"),
    ("os.name sniffing", DOMAIN_MODULE, "def f(os):\n    return os.name\n"),
    # Builtins, called or merely named.
    ("open call", DOMAIN_MODULE, "def f(name):\n    return open(name)\n"),
    ("eval call", DOMAIN_MODULE, "def f(text):\n    return eval(text)\n"),
    ("exec call", DOMAIN_MODULE, "def f(text):\n    exec(text)\n"),
    ("compile call", DOMAIN_MODULE, "def f(text):\n    return compile(text, 'x', 'exec')\n"),
    ("dunder import call", DOMAIN_MODULE, "def f(name):\n    return __import__(name)\n"),
    ("aliased import builtin", DOMAIN_MODULE, "_imp = __import__\n_imp('os')\n"),
    ("aliased open", DOMAIN_MODULE, "_reader = open\n\ndef f(p):\n    return _reader(p)\n"),
    ("open passed as a value", DOMAIN_MODULE, "def f(hook=open):\n    return hook\n"),
    ("builtins subscript", DOMAIN_MODULE, "def f(p):\n    return __builtins__['open'](p)\n"),
    ("builtins exec subscript", DOMAIN_MODULE, "def f(t):\n    __builtins__['exec'](t)\n"),
    ("builtins attribute", DOMAIN_MODULE, "def f():\n    return getattr(__builtins__, 'open')\n"),
    (
        "globals reach-through",
        DOMAIN_MODULE,
        "def f():\n    return globals()['__builtins__']['__import__']('os')\n",
    ),
    ("locals reach-through", DOMAIN_MODULE, "def f():\n    return locals()\n"),
    ("vars reach-through", DOMAIN_MODULE, "def f(o):\n    return vars(o)\n"),
    # Layers.
    (
        "absolute adapters module",
        DOMAIN_MODULE,
        "from okto_grafx.adapters.storage_local import LocalStorageDevice\n",
    ),
    ("absolute api module", DOMAIN_MODULE, "from okto_grafx.api import Database\n"),
    ("absolute cli module", DOMAIN_MODULE, "from okto_grafx.cli.main import main\n"),
    ("relative adapters module", NESTED_MODULE, "from ...adapters import storage_local\n"),
    ("package import of adapters", DOMAIN_MODULE, "from okto_grafx import adapters\n"),
    ("package import of runtime", DOMAIN_MODULE, "from okto_grafx import runtime\n"),
    ("package import of api", DOMAIN_MODULE, "from okto_grafx import api\n"),
    ("package import of cli", DOMAIN_MODULE, "from okto_grafx import cli\n"),
    ("aliased package import", DOMAIN_MODULE, "from okto_grafx import adapters as _a\n"),
    (
        "relative package import of adapters",
        "okto_grafx.domain.errors",
        "from .. import adapters\n",
    ),
    (
        "relative package import of runtime",
        "okto_grafx.domain.errors",
        "from .. import runtime\n",
    ),
    ("deep relative package import", NESTED_MODULE, "from ... import adapters\n"),
    # A12: the domain may not reach into the engine.
    (
        "domain imports the engine",
        DOMAIN_MODULE,
        "from okto_grafx.engine.buffer_pool import BufferPool\n",
    ),
    ("domain imports the engine package", DOMAIN_MODULE, "from okto_grafx import engine\n"),
    (
        "domain imports the engine relatively",
        NESTED_MODULE,
        "from ...engine import buffer_pool\n",
    ),
    # Statement imports of the package root, bare and dotted.
    ("bare package import", DOMAIN_MODULE, "import okto_grafx\n"),
    ("aliased bare package import", DOMAIN_MODULE, "import okto_grafx as grafx\n"),
    ("dotted statement import", DOMAIN_MODULE, "import okto_grafx.domain.ids\n"),
    ("runtime statement import", DOMAIN_MODULE, "import okto_grafx.runtime.registry\n"),
    ("star import of the package root", DOMAIN_MODULE, "from okto_grafx import *\n"),
    ("version from the package root", DOMAIN_MODULE, "from okto_grafx import __version__\n"),
    ("facade from the package root", DOMAIN_MODULE, "from okto_grafx import connect\n"),
    (
        "aliased facade from the package root",
        DOMAIN_MODULE,
        "from okto_grafx import Database as _Database\n",
    ),
    # TYPE_CHECKING may not be forged.
    (
        "forged guard by assignment",
        DOMAIN_MODULE,
        "TYPE_CHECKING = True\nif TYPE_CHECKING:\n    import uuid\n",
    ),
    (
        "guard reassigned after a real import",
        DOMAIN_MODULE,
        "from typing import TYPE_CHECKING\nTYPE_CHECKING = True\nif TYPE_CHECKING:\n    import uuid\n",
    ),
    (
        "guard borrowed from another module",
        DOMAIN_MODULE,
        "from okto_grafx.domain.flags import TYPE_CHECKING\nif TYPE_CHECKING:\n    import uuid\n",
    ),
    (
        "guard aliased from a non-typing module",
        DOMAIN_MODULE,
        "from okto_grafx.domain.flags import DEBUG as TYPE_CHECKING\nif TYPE_CHECKING:\n    import uuid\n",
    ),
    (
        "unqualified attribute guard",
        DOMAIN_MODULE,
        "import okto_grafx.domain.flags as flags\nif flags.TYPE_CHECKING:\n    import uuid\n",
    ),
    (
        "type checking does not excuse mechanism",
        DOMAIN_MODULE,
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import os\n",
    ),
    (
        "type checking does not excuse a forbidden layer",
        DOMAIN_MODULE,
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from okto_grafx import adapters\n",
    ),
    (
        "type checking guard does not cover its else branch",
        DOMAIN_MODULE,
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    pass\nelse:\n    import uuid\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "module_name", "source"),
    VIOLATING_SOURCES,
    ids=[row[0] for row in VIOLATING_SOURCES],
)
def test_the_checker_flags_a_violation(label: str, module_name: str, source: str) -> None:
    assert scan_source(module_name, source), label


def test_the_submodule_rule_names_the_layer_it_caught() -> None:
    violations = scan_source(DOMAIN_MODULE, "from okto_grafx import adapters\n")
    assert len(violations) == 1
    assert "okto_grafx.adapters" in violations[0]


def test_reassigning_the_guard_is_a_violation_on_its_own() -> None:
    violations = scan_source(
        DOMAIN_MODULE, "from typing import TYPE_CHECKING\nTYPE_CHECKING = True\n"
    )
    assert any("assigns to 'TYPE_CHECKING'" in message for message in violations)


ACCEPTED_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("future annotations", DOMAIN_MODULE, "from __future__ import annotations\n"),
    ("typing", DOMAIN_MODULE, "from typing import Protocol\n"),
    ("collections.abc", DOMAIN_MODULE, "from collections.abc import Mapping\n"),
    ("collections submodule", DOMAIN_MODULE, "from collections import abc\n"),
    ("dataclasses", DOMAIN_MODULE, "from dataclasses import dataclass\n"),
    ("struct", DOMAIN_MODULE, "import struct\n"),
    ("zlib", DOMAIN_MODULE, "import zlib\n"),
    ("hashlib", DOMAIN_MODULE, "from hashlib import sha256\n"),
    ("math", DOMAIN_MODULE, "from math import isfinite\n"),
    (
        "exact commit diagnostic timer",
        TXN_MANAGER_MODULE,
        "from time import perf_counter_ns\n",
    ),
    (
        "sibling domain module",
        DOMAIN_MODULE,
        "from okto_grafx.domain.errors import GrafxError\n",
    ),
    ("domain package import", DOMAIN_MODULE, "from okto_grafx.domain import ports\n"),
    ("relative sibling", NESTED_MODULE, "from .errors import GrafxError\n"),
    ("relative parent", NESTED_MODULE, "from ..ids import RecordRef\n"),
    ("relative package import inside the core", NESTED_MODULE, "from .. import ids\n"),
    (
        "the engine may import the domain",
        ENGINE_MODULE,
        "from okto_grafx.domain.ports import StorageDevice\n",
    ),
    (
        "the engine may import the engine",
        ENGINE_MODULE,
        "from okto_grafx.engine.buffer_pool import BufferPool\n",
    ),
    (
        "uuid type under a guard",
        DOMAIN_MODULE,
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from uuid import UUID\n",
    ),
    (
        "uuid type under a qualified guard",
        DOMAIN_MODULE,
        "import typing\nif typing.TYPE_CHECKING:\n    import uuid\n",
    ),
    (
        "uuid type under an aliased guard",
        DOMAIN_MODULE,
        "from typing import TYPE_CHECKING as _TC\nif _TC:\n    from uuid import UUID\n",
    ),
    ("attribute that only looks like sniffing", DOMAIN_MODULE, "def f(page):\n    return page.name\n"),
    ("method named compile", DOMAIN_MODULE, "def f(planner):\n    return planner.compile()\n"),
    ("method named open", ENGINE_MODULE, "class R:\n    def open(self):\n        return None\n"),
    (
        "attribute call named open",
        ENGINE_MODULE,
        "def f(registration):\n    return registration.open()\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "module_name", "source"),
    ACCEPTED_SOURCES,
    ids=[row[0] for row in ACCEPTED_SOURCES],
)
def test_the_checker_accepts_a_legitimate_source(label: str, module_name: str, source: str) -> None:
    assert scan_source(module_name, source) == [], label


def test_a_real_port_module_still_passes_after_the_submodule_rule() -> None:
    # The rule must not fire on ``from <domain package> import <symbol>``, which is how every
    # port module actually imports its neighbours.
    path = PACKAGE_ROOT / "domain" / "ports" / "__init__.py"
    assert scan_source(_module_name(path), path.read_text(encoding="utf-8")) == []


# --- the reverse direction is allowed ---------------------------------------------------------


def test_an_adapter_may_import_the_domain() -> None:
    # The dependency arrow points inwards: adapters and the composition root depend on the pure
    # core, never the other way round. Those layers are deliberately not scanned.
    adapter_source = (
        "from okto_grafx.domain.errors import GrafxDeviceFull\n"
        "from okto_grafx.domain.ports import StorageDevice\n"
        "import os\n"
        "import sys\n"
    )
    scanned = {path.resolve() for path in PURE_CORE_FILES}
    for layer in ("adapters", "runtime"):
        for path in (PACKAGE_ROOT / layer).rglob("*.py"):
            assert path.resolve() not in scanned
    # The very same source would be a violation inside the pure core, which is what makes the
    # asymmetry meaningful rather than accidental.
    assert scan_source(DOMAIN_MODULE, adapter_source)


def test_the_runtime_layer_really_does_import_the_domain() -> None:
    registry_source = (PACKAGE_ROOT / "runtime" / "registry.py").read_text(encoding="utf-8")
    assert "from okto_grafx.domain.ports import" in registry_source
    assert "from okto_grafx.domain.errors import" in registry_source
