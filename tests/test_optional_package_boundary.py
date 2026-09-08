"""GX-CAP-0 dependency arrows across the complete core wheel, not just pure layers.

This is a static architectural gate, not a sandbox for trusted Python. Literal
dynamic imports are checked too; arbitrary computed loading requires review.
Optional consumers get the same source checker once their packages are added.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPTIONAL = ("okto_grafx_agent", "okto_grafx_workspace", "okto_grafx_mcp",
            "okto_grafx_algorithms", "okto_grafx_interop")
INTERNAL = tuple(f"okto_grafx.{part}" for part in
                 ("engine", "domain", "runtime", "adapters", "cli", "api"))


def _inside(name: str, prefix: str) -> bool:
    return name == prefix or name.startswith(prefix + ".")


def violations(module: str, source: str, *, package: bool = False) -> list[str]:
    """Find forbidden dependency edges, including from-imported submodules."""
    core = _inside(module, "okto_grafx")
    forbidden = (
        (*OPTIONAL, "mcp", "okto_grafx.agent", "okto_grafx.mcp")
        if core else INTERNAL
    )
    if _inside(module, "okto_grafx_agent"):
        forbidden += ("okto_grafx_mcp", "mcp")
    if _inside(module, "okto_grafx_mcp"):
        forbidden += tuple(f"okto_grafx_agent.{part}" for part in
                           ("engine", "adapters", "runtime", "_internal"))
    found: list[str] = []
    tree = ast.parse(source)
    import_functions = {"__import__"}
    importlib_names = {"importlib"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "importlib":
            for alias in node.names:
                if alias.name == "import_module":
                    import_functions.add(alias.asname or alias.name)

    def check(name: str, line: int) -> None:
        if any(_inside(name, item) for item in forbidden):
            found.append(f"{line}: {name}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                check(alias.name, node.lineno)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = module.split(".") if package else module.split(".")[:-1]
                parent = parent[:len(parent) - node.level + 1]
                base = ".".join((*parent, *([base] if base else [])))
            check(base, node.lineno)
            for alias in node.names:
                check(f"{base}.{alias.name}", node.lineno)
        elif isinstance(node, ast.Call) and node.args:
            func = node.func
            loader = (
                isinstance(func, ast.Name) and func.id in import_functions
                or isinstance(func, ast.Attribute) and func.attr == "import_module"
                and isinstance(func.value, ast.Name) and func.value.id in importlib_names
            )
            if loader and isinstance(node.args[0], ast.Constant):
                value = node.args[0].value
                if isinstance(value, str):
                    check(value, node.lineno)
    return found


def test_complete_core_wheel_does_not_depend_on_optional_products() -> None:
    files = sorted((ROOT / "src" / "okto_grafx").rglob("*.py"))
    assert files and any(path.parent.name == "api" for path in files)
    failures = {}
    for path in files:
        parts = path.relative_to(ROOT / "src").with_suffix("").parts
        package = parts[-1] == "__init__"
        module = ".".join(parts[:-1] if package else parts)
        errors = violations(module, path.read_text(encoding="utf-8"), package=package)
        if errors:
            failures[str(path.relative_to(ROOT))] = errors
    assert failures == {}


@pytest.mark.parametrize("module,source,package", [
    ("okto_grafx.api.database", "import okto_grafx_agent as a", False),
    ("okto_grafx.runtime.config", "from okto_grafx_workspace import resolve", False),
    ("okto_grafx", "from . import agent", True),
    ("okto_grafx.api", "from .. import mcp", True),
    ("okto_grafx.api.database", "from mcp.server import Server", False),
    ("okto_grafx.api.database", "if TYPE_CHECKING:\n from okto_grafx_interop import Row", False),
    ("okto_grafx.adapters.x", "__import__('okto_grafx_algorithms')", False),
    ("okto_grafx.adapters.x", "import importlib as il\nil.import_module('okto_grafx_mcp')", False),
    ("okto_grafx.adapters.x", "from importlib import import_module as load\nload('mcp')", False),
    ("okto_grafx_agent.api", "from okto_grafx_mcp import server", False),
    ("okto_grafx_interop.arrow", "from okto_grafx import engine", False),
    ("okto_grafx_workspace.resolve", "from okto_grafx.runtime import config", False),
    ("okto_grafx_algorithms.rank", "import okto_grafx.domain.model", False),
    ("okto_grafx_mcp.server", "from okto_grafx_agent import _internal", False),
    ("okto_grafx_interop.arrow", "from okto_grafx.api.assembly import build", False),
    ("okto_grafx_workspace.resolve", "from okto_grafx.api import database", False),
])
def test_gate_rejects_dependency_reversals(module: str, source: str, package: bool) -> None:
    assert violations(module, source, package=package)


@pytest.mark.parametrize("module,source", [
    ("okto_grafx.api.database", "from okto_grafx.engine import query_engine"),
    ("okto_grafx.adapters.vector_numpy", "import numpy"),
    ("okto_grafx.api.database", "# import mcp\nx = 'okto_grafx_agent'"),
    ("okto_grafx_agent.api", "from okto_grafx import Database, DatabaseConfig"),
    ("okto_grafx_workspace.resolve", "from okto_grafx.errors import GrafxError"),
    ("okto_grafx_mcp.server", "from okto_grafx_agent import AgentSession"),
    ("okto_grafx_mcp.server", "import mcp.server"),
    ("okto_grafx_interop.arrow", "import pyarrow\nfrom okto_grafx import Database"),
])
def test_gate_accepts_public_consumers_and_existing_core_composition(module: str, source: str) -> None:
    assert violations(module, source) == []
