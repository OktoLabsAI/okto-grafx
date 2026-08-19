"""Static properties every module of the package must keep (CONTRACT.md section 11).

Item 3 of the definition of done asks for type annotations on every public symbol and postponed
annotation evaluation everywhere; item 8 forbids leftover stubs. Both are cheap to state as a
gate over the source tree, and stating them here keeps them true for every later wave.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
PACKAGE_ROOT: Path = PROJECT_ROOT / "src" / "okto_grafx"
SOURCE_FILES: list[Path] = sorted(PACKAGE_ROOT.rglob("*.py"))

FORBIDDEN_MARKERS: tuple[str, ...] = ("TODO", "FIXME", "XXX", "NotImplementedError")
"""Markers that say the code is unfinished. Delivered code carries none of them."""


def _relative(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")


def _is_public(name: str) -> bool:
    """Return True for a name that belongs to the public surface, dunders included."""
    if name.startswith("__") and name.endswith("__"):
        return True
    return not name.startswith("_")


def test_the_gate_sees_the_whole_package() -> None:
    assert len(SOURCE_FILES) >= 15


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_every_public_function_is_fully_annotated(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_public(node.name):
            continue
        location = f"{_relative(path)}:{node.lineno} {node.name}"
        arguments = [
            *node.args.posonlyargs,
            *node.args.args,
            *node.args.kwonlyargs,
        ]
        if node.args.vararg is not None:
            arguments.append(node.args.vararg)
        if node.args.kwarg is not None:
            arguments.append(node.args.kwarg)
        for argument in arguments:
            if argument.arg in {"self", "cls"}:
                continue
            assert argument.annotation is not None, f"{location} parameter {argument.arg}"
        assert node.returns is not None, f"{location} return value"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_every_module_postpones_annotation_evaluation(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    assert "from __future__ import annotations" in source


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_no_module_carries_an_unfinished_marker(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    for marker in FORBIDDEN_MARKERS:
        assert marker not in source, f"{_relative(path)} still carries {marker}"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_every_module_declares_what_it_exports(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = {
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    assert "__all__" in names, f"{_relative(path)} does not declare __all__"


@pytest.mark.parametrize("path", SOURCE_FILES, ids=_relative)
def test_every_declared_export_is_defined_or_imported_by_its_module(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    defined |= {
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    imported = {
        (alias.asname or alias.name).split(".")[0]
        for node in ast.walk(tree)
        for alias in (node.names if isinstance(node, (ast.Import, ast.ImportFrom)) else [])
    }
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets):
            continue
        if not isinstance(node.value, (ast.List, ast.Tuple)):
            continue
        for element in node.value.elts:
            assert isinstance(element, ast.Constant) and isinstance(element.value, str)
            assert element.value in defined or element.value in imported, (
                f"{_relative(path)} exports {element.value!r}, which it does not define or import"
            )
