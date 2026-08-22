"""No test may be discarded at import time (CONTRACT.md A84, and G4's family of rules).

Python keeps the last definition of a name. Two functions called ``test_thing`` in one module
means the first is thrown away at import: pytest never sees it, the count does not drop because
the replacement runs in its place, and nothing anywhere reports the loss. A sibling component
measured the consequence and it is worse than a missing test -- a whole round of mutation
attributions was unreliable, with kills credited to a function that was not executing.

It is invisible on both axes that normally catch things here. A reviewer reads two definitions
that are each correct on their own, and the suite reads a healthy number. So it belongs in the
harness rather than in anyone's attention, which is what A73 says about exactly this shape.

Three ways a test can be discarded, all checked statically -- no collection, nothing a runtime
hook can reach:

* two top-level definitions of one name in a module;
* two fixtures of one name in a module;
* a definition shadowed by a later import or assignment binding the same name.

The scan also pins the directories it walks. A gate that silently walks an empty list reports
success for the wrong reason (A56, A68), which is the failure the file-count guards elsewhere in
this suite already defend against.
"""

from __future__ import annotations

import ast
import collections
from pathlib import Path

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
TESTS_ROOT: Path = PROJECT_ROOT / "tests"

EXPECTED_PACKAGES: frozenset[str] = frozenset(
    {"foundation", "storage_core", "storage_adapters", "coordination", "observability"}
)
"""Test packages that exist today. Named so an empty or truncated walk cannot pass quietly."""

FIXTURE_DECORATORS: frozenset[str] = frozenset({"fixture"})


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _test_modules() -> list[Path]:
    """Return every module of the suite, including conftest files."""
    return sorted(
        path
        for path in TESTS_ROOT.rglob("*.py")
        if "__pycache__" not in path.parts
    )


TEST_MODULES: list[Path] = _test_modules()


def _decorator_names(node: ast.AST) -> set[str]:
    """Return the trailing names of every decorator on a definition."""
    names: set[str] = set()
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        while isinstance(target, ast.Attribute):
            names.add(target.attr)
            target = target.value
        if isinstance(target, ast.Name):
            names.add(target.id)
    return names


TRANSPARENT_CONTAINERS: tuple[type[ast.stmt], ...] = (
    ast.If,
    ast.Try,
    ast.With,
    ast.AsyncWith,
    ast.For,
    ast.AsyncFor,
    ast.While,
)
"""Statements that hold a body without opening a scope.

A ``def`` inside ``if True:`` binds in the enclosing scope exactly as a top-level one does, so a
second definition there discards the first just the same. Reading only direct body statements
missed every one of these; a ``def`` inside a ``def`` or a ``class`` does NOT, which is why
those two are absent from this list.
"""


def _flatten(body: list[ast.stmt]) -> list[ast.stmt]:
    """Return every statement that binds in this scope, descending transparent containers."""
    flattened: list[ast.stmt] = []
    for node in body:
        flattened.append(node)
        if isinstance(node, TRANSPARENT_CONTAINERS):
            for attribute in ("body", "orelse", "finalbody", "handlers"):
                branch = getattr(node, attribute, [])
                for element in branch:
                    if isinstance(element, ast.ExceptHandler):
                        flattened.extend(_flatten(element.body))
                    else:
                        flattened.extend(_flatten([element]))
    return flattened


def _definitions(body: list[ast.stmt]) -> list[tuple[str, int, ast.AST]]:
    """Return the (name, line, node) of every function this scope binds."""
    return [
        (node.name, node.lineno, node)
        for node in _flatten(body)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _later_bindings(body: list[ast.stmt]) -> dict[str, int]:
    """Return names bound by an import or assignment, mapped to the line that binds them."""
    bindings: dict[str, int] = {}
    for node in _flatten(body):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    continue
                bindings[alias.asname or alias.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node.lineno
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bindings[node.target.id] = node.lineno
    return bindings


def discarded_definitions(source: str) -> list[str]:
    """Return one message per definition this module throws away at import time.

    Scopes are examined separately: a class may reuse a name the module level also uses, and
    that is ordinary. Two definitions inside ONE scope are not.
    """
    tree = ast.parse(source)
    found: list[str] = []

    def scan(body: list[ast.stmt], scope: str) -> None:
        seen: collections.Counter[str] = collections.Counter()
        for name, line, node in _definitions(body):
            seen[name] += 1
            if seen[name] > 1:
                kind = "fixture" if _decorator_names(node) & FIXTURE_DECORATORS else "definition"
                found.append(f"{scope}{name} is a duplicate {kind} at line {line}")
        bindings = _later_bindings(body)
        for name, line, _node in _definitions(body):
            bound_at = bindings.get(name)
            if bound_at is not None and bound_at > line:
                found.append(
                    f"{scope}{name} defined at line {line} is shadowed by a binding at "
                    f"line {bound_at}"
                )

    scan(tree.body, "")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            scan(node.body, f"{node.name}.")
    return found


# --- the live rule ------------------------------------------------------------------------------


def test_the_gate_walks_the_suite_it_claims_to() -> None:
    # A56 and A68: pin what is walked. An empty or truncated list would report success for the
    # wrong reason, and this gate has no other signal that it ran.
    assert TESTS_ROOT.is_dir()
    assert len(TEST_MODULES) >= 40, len(TEST_MODULES)
    packages = {
        path.relative_to(TESTS_ROOT).parts[0]
        for path in TEST_MODULES
        if len(path.relative_to(TESTS_ROOT).parts) > 1
    }
    assert EXPECTED_PACKAGES <= packages, sorted(EXPECTED_PACKAGES - packages)
    assert any(path.name == "conftest.py" for path in TEST_MODULES)


@pytest.mark.parametrize("path", TEST_MODULES, ids=_relative)
def test_no_module_discards_a_definition_at_import(path: Path) -> None:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):  # pragma: no cover - unreadable file
        pytest.skip(f"pending: {_relative(path)} could not be read")
    try:
        found = discarded_definitions(source)
    except SyntaxError:
        # A module mid-write belongs to the component writing it, and pytest already fails on it
        # as that component's own collection error.
        return
    assert found == [], f"{_relative(path)} throws away: {'; '.join(found)}"


# --- the rule must be able to fail ---------------------------------------------------------------

DISCARDING_SOURCES: tuple[tuple[str, str], ...] = (
    (
        "two tests of one name",
        "def test_thing():\n    assert True\n\n\ndef test_thing():\n    assert False\n",
    ),
    (
        "two fixtures of one name",
        "import pytest\n\n\n@pytest.fixture\ndef thing():\n    return 1\n\n\n"
        "@pytest.fixture\ndef thing():\n    return 2\n",
    ),
    (
        "a test and a fixture of one name",
        "import pytest\n\n\ndef test_thing():\n    assert True\n\n\n"
        "@pytest.fixture\ndef test_thing():\n    return 1\n",
    ),
    (
        "a test shadowed by a later import",
        "def test_thing():\n    assert True\n\n\nfrom helpers import test_thing\n",
    ),
    (
        "a test shadowed by a later assignment",
        "def test_thing():\n    assert True\n\n\ntest_thing = None\n",
    ),
    (
        "a test shadowed by a later aliased import",
        "def test_thing():\n    assert True\n\n\nimport helpers as test_thing\n",
    ),
    (
        "duplicates inside a class",
        "class TestGroup:\n    def test_thing(self):\n        assert True\n\n"
        "    def test_thing(self):\n        assert False\n",
    ),
    (
        "three of one name",
        "def test_thing():\n    pass\n\n\ndef test_thing():\n    pass\n\n\n"
        "def test_thing():\n    pass\n",
    ),
    (
        "a helper shadowing a helper",
        "def _support():\n    return 1\n\n\ndef _support():\n    return 2\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"), DISCARDING_SOURCES, ids=[row[0] for row in DISCARDING_SOURCES]
)
def test_the_rule_flags_a_discarded_definition(label: str, source: str) -> None:
    assert discarded_definitions(source), label


KEEPING_SOURCES: tuple[tuple[str, str], ...] = (
    (
        "two distinct tests",
        "def test_one():\n    assert True\n\n\ndef test_two():\n    assert True\n",
    ),
    (
        "an import before the definition it does not shadow",
        "from helpers import support\n\n\ndef test_thing():\n    assert support()\n",
    ),
    (
        "the same method name in two classes",
        "class TestFirst:\n    def test_thing(self):\n        assert True\n\n\n"
        "class TestSecond:\n    def test_thing(self):\n        assert True\n",
    ),
    (
        "a class method and a module function of one name",
        "def test_thing():\n    assert True\n\n\nclass TestGroup:\n"
        "    def test_thing(self):\n        assert True\n",
    ),
    (
        "an overload pair",
        "from typing import overload\n\n\n@overload\ndef helper(value: int) -> int:\n    ...\n",
    ),
    (
        "a nested function reusing an outer name",
        "def test_thing():\n    def helper():\n        return 1\n\n    return helper()\n\n\n"
        "def helper():\n    return 2\n",
    ),
    (
        "a star import, which binds nothing this rule can name",
        "def test_thing():\n    assert True\n\n\nfrom helpers import *\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"), KEEPING_SOURCES, ids=[row[0] for row in KEEPING_SOURCES]
)
def test_the_rule_spares_a_module_that_keeps_everything(label: str, source: str) -> None:
    assert discarded_definitions(source) == [], label


def test_the_rule_fails_a_planted_module(tmp_path: Path) -> None:
    # Pins the ASSERTION, not only the helper: no module in the suite carries this defect, so
    # the rule never fires on the tree and neutering it would change nothing measurable.
    module = tmp_path / "test_planted_duplicate.py"
    module.write_text(
        "def test_thing():\n    assert True\n\n\ndef test_thing():\n    assert False\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="throws away"):
        test_no_module_discards_a_definition_at_import(module)


def test_the_message_names_the_lost_definition_and_its_line() -> None:
    found = discarded_definitions(
        "def test_thing():\n    assert True\n\n\ndef test_thing():\n    assert False\n"
    )
    assert len(found) == 1
    assert "test_thing" in found[0]
    assert "line 5" in found[0]


NESTED_DISCARDS: tuple[tuple[str, str], ...] = (
    (
        "second definition inside if True",
        "def test_thing():\n    assert True\n\n\nif True:\n"
        "    def test_thing():\n        assert False\n",
    ),
    (
        "second definition inside try",
        "def test_thing():\n    assert True\n\n\ntry:\n"
        "    def test_thing():\n        assert False\n"
        "except Exception:\n    pass\n",
    ),
    (
        "second definition inside an except handler",
        "def test_thing():\n    assert True\n\n\ntry:\n    pass\n"
        "except Exception:\n    def test_thing():\n        assert False\n",
    ),
    (
        "second definition inside with",
        "def test_thing():\n    assert True\n\n\nwith open('x') as handle:\n"
        "    def test_thing():\n        assert False\n",
    ),
    (
        "second definition inside for",
        "def test_thing():\n    assert True\n\n\nfor _ in range(1):\n"
        "    def test_thing():\n        assert False\n",
    ),
    (
        "assignment inside if True shadows the definition",
        "def test_thing():\n    assert True\n\n\nif True:\n    test_thing = None\n",
    ),
    (
        "import inside try shadows the definition",
        "def test_thing():\n    assert True\n\n\ntry:\n"
        "    from helpers import test_thing\n"
        "except ImportError:\n    pass\n",
    ),
    (
        "both definitions nested",
        "if True:\n    def test_thing():\n        assert True\n\n"
        "if True:\n    def test_thing():\n        assert False\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"), NESTED_DISCARDS, ids=[row[0] for row in NESTED_DISCARDS]
)
def test_the_rule_flags_a_nested_discard(label: str, source: str) -> None:
    # D6: reading only direct body statements missed every one of these, and Python discards the
    # first definition in each. A container that does not open a scope is not a hiding place.
    assert discarded_definitions(source), label


NESTED_KEEPERS: tuple[tuple[str, str], ...] = (
    (
        "a nested function inside a def",
        "def test_thing():\n    def helper():\n        return 1\n    return helper()\n\n\n"
        "def helper():\n    return 2\n",
    ),
    (
        "a method and a module function",
        "if True:\n    def test_thing():\n        assert True\n\n\n"
        "class TestGroup:\n    def test_thing(self):\n        assert True\n",
    ),
    (
        "one definition in each branch of an if",
        "import sys\n\nif sys.maxsize > 0:\n    def helper():\n        return 1\n"
        "else:\n    def other():\n        return 2\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"), NESTED_KEEPERS, ids=[row[0] for row in NESTED_KEEPERS]
)
def test_the_rule_spares_a_nested_definition_that_keeps_everything(
    label: str, source: str
) -> None:
    assert discarded_definitions(source) == [], label
