"""Windows and POSIX are equal citizens: the counterpart rule (CONTRACT.md G4, amendment A32).

G4 is enforced in two halves, and this file is the smaller one.

The **dynamic half** lives in ``tests/conftest.py`` and is authoritative. It records every skip
the run actually produces, however it was produced -- a decorator, a fixture, a helper three
calls deep, a ``pytest_runtest_setup`` hook, a module-level skip -- and fails the session unless
each one is attributable to ``@pytest.mark.platform_specific`` or to a declared non-platform
reason. Predicting skips from source proved undecidable across four audits; observing outcomes
cannot be laundered, because it never has to guess.

The **static half** is this file, and it answers the one question runtime cannot: only one
operating-system family runs at a time, so a run can never observe whether the *other* family is
covered. So the rule kept here is exactly one -- **a ``platform_specific`` test declares the
family it runs on with a ``skipif`` condition, and within its module both polarities of that
condition exist.** Everything that tried to predict skips has been deleted rather than patched.

Two supporting rules make that one honest: a marked test must actually carry a condition, and a
counterpart that is itself unconditionally skipped does not count as covering its family -- an
``@pytest.mark.skip`` on the Windows branch would otherwise let a POSIX-only module claim parity.

The file set matches pytest's own collection rather than a narrower glob: the ``python_files``
patterns from the configuration plus ``conftest.py``, so a module named ``parity_test.py`` is
read here exactly as pytest would collect it.
"""

from __future__ import annotations

import ast
import fnmatch
import importlib.util
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
TESTS_ROOT: Path = PROJECT_ROOT / "tests"
PYPROJECT: Path = PROJECT_ROOT / "pyproject.toml"

DEFAULT_PYTHON_FILES: tuple[str, ...] = ("test_*.py", "*_test.py")
"""What pytest collects when nothing configures it otherwise."""

MARKER: str = "platform_specific"

def _shared_rules() -> object:
    """Load the family rule from tests/conftest.py, so both halves use ONE definition.

    A32 forbids the two halves of G4 from disagreeing about what a correct spelling is, and they
    have disagreed twice: once about string conditions, once about what counts as naming a
    family. A shared constant with an equality test would still be two definitions; importing
    the module that owns the rule is one.
    """
    location = PROJECT_ROOT / "tests" / "conftest.py"
    spec = importlib.util.spec_from_file_location("_c0_shared_rules", location)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_c0_shared_rules"] = module
    spec.loader.exec_module(module)
    return module


SHARED = _shared_rules()
PLATFORM_VALUES: frozenset[str] = SHARED.PLATFORM_VALUES
_condition_names_a_family = SHARED._condition_names_a_family

PLATFORM_ATTRIBUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("sys", "platform"),
        ("os", "name"),
        ("os", "uname"),
        ("platform", "system"),
        ("platform", "machine"),
        ("platform", "platform"),
    }
)
"""Direct readings of the running operating system."""

PLATFORM_NAME_HINTS: tuple[str, ...] = (
    "WINDOWS",
    "WIN32",
    "POSIX",
    "LINUX",
    "DARWIN",
    "MACOS",
    "CYGWIN",
    "PLATFORM",
)
"""Spellings that make a name a platform flag, wherever it was defined or imported from."""


_UNRESOLVED: object = object()

_COMPARISONS = {
    ast.Gt: lambda left, right: left > right,
    ast.GtE: lambda left, right: left >= right,
    ast.Lt: lambda left, right: left < right,
    ast.LtE: lambda left, right: left <= right,
    ast.Eq: lambda left, right: left == right,
    ast.NotEq: lambda left, right: left != right,
    ast.Is: lambda left, right: left is right,
    ast.IsNot: lambda left, right: left is not right,
}


def _constant_value(node: ast.expr) -> object:
    """Fold an expression to a constant when it has one, without evaluating anything.

    A37: a counterpart that cannot run does not satisfy the pairing rule, and ``skipif(1)``,
    ``skipif(bool(1))``, ``skipif(2 > 1)`` and ``skipif(not False)`` are all as dead as
    ``skipif(True)``. Folding covers them by shape rather than by listing spellings.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _constant_value(node.operand)
        return _UNRESOLVED if inner is _UNRESOLVED else (not inner)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "bool"
        and len(node.args) == 1
        and not node.keywords
    ):
        inner = _constant_value(node.args[0])
        return _UNRESOLVED if inner is _UNRESOLVED else bool(inner)
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        left = _constant_value(node.left)
        right = _constant_value(node.comparators[0])
        if left is _UNRESOLVED or right is _UNRESOLVED:
            return _UNRESOLVED
        comparison = _COMPARISONS.get(type(node.ops[0]))
        if comparison is None:
            return _UNRESOLVED
        try:
            return comparison(left, right)
        except TypeError:
            return _UNRESOLVED
    if isinstance(node, ast.BoolOp):
        # Short-circuit, like Python: ``True or anything`` is True whether or not the right side
        # can be folded, and that is exactly the spelling a dead counterpart hides behind.
        decisive = isinstance(node.op, ast.Or)
        for value in node.values:
            folded = _constant_value(value)
            if folded is _UNRESOLVED:
                return _UNRESOLVED
            if bool(folded) is decisive:
                return decisive
        return not decisive
    return _UNRESOLVED


def _condition_node(argument: ast.expr) -> ast.expr | None:
    """Return the expression a condition argument stands for, string conditions included.

    pytest evaluates a string condition as Python source in the module namespace, so
    ``skipif("sys.platform == 'win32'")`` is a real and correctly paired spelling. A string that
    is not valid source is returned as None: pytest would fail to evaluate it, and an unusable
    condition cannot vouch for a family.
    """
    if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
        return argument
    try:
        parsed = ast.parse(argument.value, mode="eval").body
    except SyntaxError:
        return None
    return parsed


@dataclass(frozen=True)
class Condition:
    """One platform condition, split into the expression and the polarity it is used with."""

    base: str
    negated: bool


@dataclass(frozen=True)
class MarkedTest:
    """A platform_specific test and what it declares about the family it runs on."""

    module: str
    name: str
    line: int
    conditions: tuple[Condition, ...]
    unconditionally_skipped: bool


@dataclass(frozen=True)
class ModuleReport:
    """The platform_specific tests of one module."""

    path: Path
    tests: tuple[MarkedTest, ...]


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _python_files() -> tuple[str, ...]:
    """Return the collection patterns pytest itself uses, from the configuration."""
    try:
        manifest = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):  # pragma: no cover - the manifest is in the repo
        return DEFAULT_PYTHON_FILES
    configured = manifest.get("tool", {}).get("pytest", {}).get("ini_options", {}).get(
        "python_files"
    )
    if configured is None:
        return DEFAULT_PYTHON_FILES
    if isinstance(configured, str):
        return tuple(configured.split())
    return tuple(configured)


COLLECTION_PATTERNS: tuple[str, ...] = _python_files()


def _collected_files() -> list[Path]:
    """Return every module pytest would read: the test patterns plus every conftest."""
    files: set[Path] = set()
    for path in TESTS_ROOT.rglob("*.py"):
        name = path.name
        if name == "conftest.py" or any(
            fnmatch.fnmatch(name, pattern) for pattern in COLLECTION_PATTERNS
        ):
            files.add(path)
    return sorted(files)


def _dotted(node: ast.expr) -> str:
    """Return the dotted spelling of an attribute chain, or an empty string."""
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return ""


def _decorator_name(decorator: ast.expr) -> str:
    """Return the dotted name of a decorator, whether it is called or not."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return _dotted(target)


def _is_platform_expression(node: ast.AST) -> bool:
    """Return True when the expression reads the operating system in any recognised way."""
    for element in ast.walk(node):
        if isinstance(element, ast.Attribute):
            value = element.value
            if isinstance(value, ast.Name) and (value.id, element.attr) in PLATFORM_ATTRIBUTES:
                return True
        if isinstance(element, ast.Name):
            if any(hint in element.id.upper() for hint in PLATFORM_NAME_HINTS):
                return True
    return False


def _condition_of(node: ast.expr) -> Condition:
    """Split a condition into its base expression and its polarity.

    ``not X``, ``X != "win32"``, ``X == "win32"``, ``X is True`` and ``X is False`` are five
    spellings of two polarities of one question. All five are normalised, because a gate that
    fails a correctly paired module teaches authors to route around it.
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        inner = _condition_of(node.operand)
        return Condition(base=inner.base, negated=not inner.negated)
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        operator = node.ops[0]
        comparator = node.comparators[0]
        if isinstance(operator, (ast.Is, ast.IsNot)) and isinstance(comparator, ast.Constant):
            if comparator.value is True or comparator.value is False:
                inner = _condition_of(node.left)
                truthy = comparator.value is True
                if isinstance(operator, ast.IsNot):
                    truthy = not truthy
                return Condition(base=inner.base, negated=inner.negated ^ (not truthy))
        if isinstance(operator, (ast.Eq, ast.NotEq)):
            base = f"{ast.unparse(node.left)} == {ast.unparse(comparator)}"
            return Condition(base=base, negated=isinstance(operator, ast.NotEq))
    return Condition(base=ast.unparse(node), negated=False)


def _markers_of(node: ast.AST) -> list[ast.expr]:
    """Return the decorator expressions of a definition."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return list(node.decorator_list)
    return []


def _module_markers(tree: ast.Module) -> list[ast.expr]:
    """Return the marker expressions a module applies to every test through pytestmark."""
    markers: list[ast.expr] = []
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in statement.targets
        ):
            continue
        value = statement.value
        if isinstance(value, (ast.List, ast.Tuple)):
            markers.extend(value.elts)
        else:
            markers.append(value)
    return markers


def _is_marked(markers: list[ast.expr]) -> bool:
    """Return True when one of the markers is pytest.mark.platform_specific."""
    return any(_decorator_name(marker).endswith(f"mark.{MARKER}") for marker in markers)


def _platform_conditions(
    markers: list[ast.expr], path: Path | None = None, module_source: str | None = None
) -> list[Condition]:
    """Return the platform conditions a marker set declares through skipif."""
    conditions: list[Condition] = []
    for marker in markers:
        if not isinstance(marker, ast.Call):
            continue
        if _decorator_name(marker).rsplit(".", 1)[-1] != "skipif":
            continue
        arguments = list(marker.args) + [
            keyword.value for keyword in marker.keywords if keyword.arg == "condition"
        ]
        for argument in arguments:
            resolved = _condition_node(argument)
            if resolved is None:
                continue
            # A89: naming a family means comparing a platform reading against a real platform
            # value, resolved through whatever name holds it. Merely MENTIONING a platform let
            # sys.platform != "" pass as a family condition while covering none.
            if not _condition_names_a_family(argument, str(path or ""), module_source):
                continue
            if _constant_value(resolved) is not _UNRESOLVED:
                # A constant condition names no family; it only says always or never.
                continue
            conditions.append(_condition_of(resolved))
    return conditions


def _is_unconditionally_skipped(markers: list[ast.expr]) -> bool:
    """Return True when a marker set disables the test outright.

    A test that never runs cannot cover its family, so it must not satisfy the counterpart rule
    for the other one.
    """
    for marker in markers:
        tail = _decorator_name(marker).rsplit(".", 1)[-1]
        if tail == "skip":
            return True
        if not isinstance(marker, ast.Call):
            continue
        if tail == "xfail":
            for keyword in marker.keywords:
                if keyword.arg == "run" and _constant_value(keyword.value) is False:
                    return True
            continue
        if tail != "skipif":
            continue
        arguments = list(marker.args) + [
            keyword.value for keyword in marker.keywords if keyword.arg == "condition"
        ]
        for argument in arguments:
            resolved = _condition_node(argument)
            if resolved is None:
                # An unusable string condition: pytest cannot evaluate it, so the test is not a
                # counterpart for anything.
                return True
            value = _constant_value(resolved)
            if value is not _UNRESOLVED and value:
                return True
            if isinstance(resolved, ast.Name) and isinstance(argument, ast.Constant):
                # A string condition naming a bare identifier evaluates in the module namespace
                # and usually raises; it cannot be trusted to run.
                return True
    return False


def analyse(path: Path, source: str) -> ModuleReport:
    """Return the platform_specific tests one module declares."""
    tree = ast.parse(source, filename=str(path))
    module_markers = _module_markers(tree)
    tests: list[MarkedTest] = []

    def visit(container: ast.AST, markers: list[ast.expr]) -> None:  # noqa: C901
        for child in ast.iter_child_nodes(container):
            if isinstance(child, ast.ClassDef):
                visit(child, markers + _markers_of(child))
                continue
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not child.name.startswith("test_"):
                continue
            own = markers + _markers_of(child)
            if not _is_marked(own):
                continue
            tests.append(
                MarkedTest(
                    module=_relative(path),
                    name=child.name,
                    line=child.lineno,
                    conditions=tuple(_platform_conditions(own, path, source)),
                    unconditionally_skipped=_is_unconditionally_skipped(own),
                )
            )

    visit(tree, module_markers)
    return ModuleReport(path=path, tests=tuple(tests))


def _read(path: Path) -> ModuleReport | None:
    try:
        return analyse(path, path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return None


COLLECTED_FILES: list[Path] = _collected_files()
UNPARSEABLE: list[str] = [_relative(path) for path in COLLECTED_FILES if _read(path) is None]
REPORTS: list[ModuleReport] = [
    report for report in map(_read, COLLECTED_FILES) if report is not None
]
MARKED_TESTS: list[MarkedTest] = [test for report in REPORTS for test in report.tests]
MARKED_REPORTS: list[ModuleReport] = [report for report in REPORTS if report.tests]


# --- the rule ---------------------------------------------------------------------------------


def test_the_gate_reads_what_pytest_collects() -> None:
    # A gate that walked a narrower glob than pytest would miss a module pytest runs.
    assert "test_*.py" in COLLECTION_PATTERNS
    assert "*_test.py" in COLLECTION_PATTERNS
    assert len(COLLECTED_FILES) >= 10
    assert any(path.name == "conftest.py" for path in COLLECTED_FILES)
    assert MARKED_TESTS, "no platform_specific test exists, so G4 has nothing to enforce"


@pytest.mark.pending
def test_no_module_of_the_suite_was_left_unread() -> None:
    # Non-blocking on purpose: a module that does not parse belongs to the component writing
    # it, and pytest already fails on it as that component's collection error. Naming it here
    # keeps the exclusion visible instead of silent. The skip is attributed by the source-visible
    # @pytest.mark.pending above -- C0 pays the same declared price it charges everyone else,
    # rather than leaning on a reason string the way A35 forbids.
    if UNPARSEABLE:
        pytest.skip(f"{len(UNPARSEABLE)} test module(s) could not be parsed: {UNPARSEABLE}")


@pytest.mark.parametrize(
    "test", MARKED_TESTS, ids=[f"{test.module}::{test.name}" for test in MARKED_TESTS]
)
def test_a_platform_specific_test_declares_the_family_it_runs_on(test: MarkedTest) -> None:
    assert test.conditions, (
        f"{test.module}:{test.line} {test.name} is marked {MARKER} but has no platform skipif "
        f"condition, so its counterpart cannot be checked"
    )


@pytest.mark.parametrize(
    "report", MARKED_REPORTS, ids=[_relative(report.path) for report in MARKED_REPORTS]
)
def test_every_family_specific_test_has_a_counterpart(report: ModuleReport) -> None:
    polarities: dict[str, set[bool]] = {}
    for test in report.tests:
        if test.unconditionally_skipped:
            # It never runs, so it covers nothing; its conditions must not close a pair.
            continue
        for condition in test.conditions:
            polarities.setdefault(condition.base, set()).add(condition.negated)

    assert polarities, (
        f"{_relative(report.path)} marks tests {MARKER} but none of them runs with a declared "
        f"platform condition"
    )
    for base, seen in sorted(polarities.items()):
        assert seen == {True, False}, (
            f"{_relative(report.path)} covers only one family of {base!r}: "
            f"the counterpart for the other family is missing (CONTRACT.md G4)"
        )


def _conditionally_defined_tests(tree: ast.Module) -> list[tuple[str, int]]:
    """Return tests whose ``def`` sits under a platform condition, so they may never exist.

    The sixth way a test stops running, and the one outcome-watching cannot see: a node id that
    is never collected on one family produces no report to reconcile and no module to miss,
    because the module is still there. Only the source shows it.
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or not _is_platform_expression(node.test):
            continue
        for branch in (node.body, node.orelse):
            for statement in branch:
                for element in ast.walk(statement):
                    if isinstance(element, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if element.name.startswith("test_"):
                            found.append((element.name, element.lineno))
    return found


@pytest.mark.parametrize(
    "path", COLLECTED_FILES, ids=[_relative(path) for path in COLLECTED_FILES]
)
def test_no_test_is_defined_only_on_one_family(path: Path) -> None:
    report = _read(path)
    if report is None:
        return
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    conditional = _conditionally_defined_tests(tree)
    assert conditional == [], (
        f"{_relative(path)} defines {conditional} inside a platform condition, so the test does "
        f"not exist on the other family and nothing can observe its absence"
    )


def test_the_conditional_definition_rule_can_fail() -> None:
    source = (
        "import sys\n\n"
        'if sys.platform == "win32":\n'
        "    def test_windows_only():\n        pass\n"
    )
    found = _conditionally_defined_tests(ast.parse(source))
    assert found and found[0][0] == "test_windows_only"


def test_the_conditional_definition_rule_spares_an_ordinary_test() -> None:
    source = "def test_thing():\n    assert True\n"
    assert _conditionally_defined_tests(ast.parse(source)) == []
    guarded = (
        "import sys\n\n"
        "def test_thing():\n"
        '    if sys.platform == "win32":\n'
        "        assert True\n"
    )
    assert _conditionally_defined_tests(ast.parse(guarded)) == []


def test_the_suite_covers_both_families_somewhere() -> None:
    polarities = {
        condition.negated
        for test in MARKED_TESTS
        if not test.unconditionally_skipped
        for condition in test.conditions
    }
    assert polarities == {True, False}


# --- the rule must be able to fail --------------------------------------------------------------


FLAG_PREAMBLE: str = 'import os\n\nIS_WINDOWS = os.name == "nt"\n\n\n'
"""Probes declare their family flag the way the real suite does (A89)."""


def _probe(source: str, *, name: str = "test_probe.py") -> ModuleReport:
    """Run the analysis over a synthetic module, so every rule can be proved to fire."""
    return analyse(TESTS_ROOT / name, FLAG_PREAMBLE + source)


PAIRED_MODULES: tuple[tuple[str, str], ...] = (
    (
        "not spelling",
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(not IS_WINDOWS, reason="windows only")\n'
        "def test_windows():\n    pass\n\n\n"
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(IS_WINDOWS, reason="posix only")\n'
        "def test_posix():\n    pass\n",
    ),
    (
        "equality spelling",
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(sys.platform == "win32", reason="posix only")\n'
        "def test_posix():\n    pass\n\n\n"
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(sys.platform != "win32", reason="windows only")\n'
        "def test_windows():\n    pass\n",
    ),
    (
        "marked through a class",
        "@pytest.mark.platform_specific\n"
        "class TestBoth:\n"
        '    @pytest.mark.skipif(IS_WINDOWS, reason="posix only")\n'
        "    def test_posix(self):\n        pass\n\n"
        '    @pytest.mark.skipif(not IS_WINDOWS, reason="windows only")\n'
        "    def test_windows(self):\n        pass\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"), PAIRED_MODULES, ids=[row[0] for row in PAIRED_MODULES]
)
def test_a_correctly_paired_module_passes_however_it_is_spelled(label: str, source: str) -> None:
    test_every_family_specific_test_has_a_counterpart(_probe(source))


LONELY_MODULES: tuple[tuple[str, str], ...] = (
    (
        "only windows",
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(not IS_WINDOWS, reason="windows only")\n'
        "def test_windows():\n    pass\n",
    ),
    (
        "only posix",
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(sys.platform == "win32", reason="posix only")\n'
        "def test_posix():\n    pass\n",
    ),
    (
        "counterpart is unconditionally skipped",
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(IS_WINDOWS, reason="posix only")\n'
        "def test_posix():\n    pass\n\n\n"
        "@pytest.mark.skip(reason=\"flaky, fix later\")\n"
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(not IS_WINDOWS, reason="windows only")\n'
        "def test_windows():\n    pass\n",
    ),
    (
        "counterpart is skipped by a constant condition",
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(IS_WINDOWS, reason="posix only")\n'
        "def test_posix():\n    pass\n\n\n"
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(True, reason="disabled")\n'
        '@pytest.mark.skipif(not IS_WINDOWS, reason="windows only")\n'
        "def test_windows():\n    pass\n",
    ),
    (
        "two tests for the same family",
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(IS_WINDOWS, reason="posix only")\n'
        "def test_posix_one():\n    pass\n\n\n"
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(IS_WINDOWS, reason="posix only")\n'
        "def test_posix_two():\n    pass\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"), LONELY_MODULES, ids=[row[0] for row in LONELY_MODULES]
)
def test_a_module_covering_one_family_only_is_rejected(label: str, source: str) -> None:
    with pytest.raises(AssertionError):
        test_every_family_specific_test_has_a_counterpart(_probe(source))


def test_a_marked_test_without_a_condition_is_rejected() -> None:
    report = _probe(
        "@pytest.mark.platform_specific\ndef test_thing():\n    pass\n"
    )
    assert len(report.tests) == 1
    with pytest.raises(AssertionError, match="no platform skipif condition"):
        test_a_platform_specific_test_declares_the_family_it_runs_on(report.tests[0])


def test_the_polarity_reader_understands_every_spelling() -> None:
    def condition(text: str) -> Condition:
        return _condition_of(ast.parse(text, mode="eval").body)

    assert condition("IS_WINDOWS") == Condition("IS_WINDOWS", False)
    assert condition("not IS_WINDOWS") == Condition("IS_WINDOWS", True)
    assert condition("not not IS_WINDOWS") == Condition("IS_WINDOWS", False)
    assert condition("IS_WINDOWS is True") == Condition("IS_WINDOWS", False)
    assert condition("IS_WINDOWS is False") == Condition("IS_WINDOWS", True)
    assert condition("IS_WINDOWS is not True") == Condition("IS_WINDOWS", True)
    assert condition("not (IS_WINDOWS is False)") == Condition("IS_WINDOWS", False)
    equal = condition('sys.platform == "win32"')
    unequal = condition('sys.platform != "win32"')
    assert equal.base == unequal.base
    assert equal.negated is False and unequal.negated is True


def test_an_unconditional_skip_is_recognised_in_every_spelling() -> None:
    def markers(text: str) -> list[ast.expr]:
        module = ast.parse(text + "\ndef test_thing():\n    pass\n")
        function = module.body[-1]
        assert isinstance(function, ast.FunctionDef)
        return function.decorator_list

    assert _is_unconditionally_skipped(markers("@pytest.mark.skip"))
    assert _is_unconditionally_skipped(markers('@pytest.mark.skip(reason="later")'))
    assert _is_unconditionally_skipped(markers('@pytest.mark.skipif(True, reason="off")'))
    assert not _is_unconditionally_skipped(markers("@pytest.mark.platform_specific"))
    assert not _is_unconditionally_skipped(
        markers('@pytest.mark.skipif(IS_WINDOWS, reason="posix only")')
    )


def test_the_collection_patterns_include_a_module_named_for_the_other_convention() -> None:
    # A module called parity_test.py is collected by pytest, so it is read here too.
    assert any(fnmatch.fnmatch("parity_test.py", pattern) for pattern in COLLECTION_PATTERNS)
    assert any(fnmatch.fnmatch("test_parity.py", pattern) for pattern in COLLECTION_PATTERNS)


def test_the_real_suite_pairs_every_family_specific_module() -> None:
    # The live assertion, stated once over the whole suite rather than only per module.
    assert MARKED_REPORTS
    for report in MARKED_REPORTS:
        test_every_family_specific_test_has_a_counterpart(report)


DEAD_COUNTERPARTS: tuple[tuple[str, str], ...] = (
    ("literal one", "@pytest.mark.skipif(1, reason='off')"),
    ("truthy string", "@pytest.mark.skipif('yes', reason='off')"),
    ("bool call", "@pytest.mark.skipif(bool(1), reason='off')"),
    ("constant comparison", "@pytest.mark.skipif(2 > 1, reason='off')"),
    ("double negative", "@pytest.mark.skipif(not False, reason='off')"),
    ("boolean operator", "@pytest.mark.skipif(True or IS_WINDOWS, reason='off')"),
    ("xfail that never runs", "@pytest.mark.xfail(run=False, reason='off')"),
    ("plain skip", "@pytest.mark.skip(reason='off')"),
    ("literal true", "@pytest.mark.skipif(True, reason='off')"),
)


@pytest.mark.parametrize(
    ("label", "decorator"), DEAD_COUNTERPARTS, ids=[row[0] for row in DEAD_COUNTERPARTS]
)
def test_a_dead_counterpart_does_not_close_the_pair(label: str, decorator: str) -> None:
    # A37: any truthy constant condition, not only the literal True. The Windows branch below
    # would never run, so the module covers POSIX only.
    source = (
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(IS_WINDOWS, reason="posix only")\n'
        "def test_posix():\n    pass\n\n\n"
        f"{decorator}\n"
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(not IS_WINDOWS, reason="windows only")\n'
        "def test_windows():\n"
        '    raise AssertionError("this is dead")\n'
    )
    with pytest.raises(AssertionError, match="counterpart|none of them runs"):
        test_every_family_specific_test_has_a_counterpart(_probe(source))


LIVE_COUNTERPARTS: tuple[tuple[str, str], ...] = (
    ("falsy constant", "@pytest.mark.skipif(False, reason='never')"),
    ("falsy comparison", "@pytest.mark.skipif(1 > 2, reason='never')"),
    ("xfail that still runs", "@pytest.mark.xfail(reason='known issue')"),
    ("an unrelated marker", "@pytest.mark.slow"),
)


@pytest.mark.parametrize(
    ("label", "decorator"), LIVE_COUNTERPARTS, ids=[row[0] for row in LIVE_COUNTERPARTS]
)
def test_a_live_counterpart_still_closes_the_pair(label: str, decorator: str) -> None:
    source = (
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(IS_WINDOWS, reason="posix only")\n'
        "def test_posix():\n    pass\n\n\n"
        f"{decorator}\n"
        "@pytest.mark.platform_specific\n"
        '@pytest.mark.skipif(not IS_WINDOWS, reason="windows only")\n'
        "def test_windows():\n    pass\n"
    )
    test_every_family_specific_test_has_a_counterpart(_probe(source))


def test_a_string_condition_is_understood_like_the_expression_it_holds() -> None:
    # pytest evaluates a string condition as source, so a module written that way is correct and
    # must not be failed for its spelling.
    source = (
        "@pytest.mark.platform_specific\n"
        "@pytest.mark.skipif(\"sys.platform == 'win32'\", reason='posix only')\n"
        "def test_posix():\n    pass\n\n\n"
        "@pytest.mark.platform_specific\n"
        "@pytest.mark.skipif(\"sys.platform != 'win32'\", reason='windows only')\n"
        "def test_windows():\n    pass\n"
    )
    report = _probe(source)
    assert all(test.conditions for test in report.tests)
    test_every_family_specific_test_has_a_counterpart(report)


def test_a_constant_condition_names_no_family() -> None:
    source = (
        "@pytest.mark.platform_specific\n"
        "@pytest.mark.skipif(True, reason='off')\n"
        "def test_nothing():\n    pass\n"
    )
    report = _probe(source)
    assert report.tests[0].conditions == ()
    assert report.tests[0].unconditionally_skipped is True


def test_the_constant_folder_covers_the_amendment_list() -> None:
    def folded(text: str) -> object:
        return _constant_value(ast.parse(text, mode="eval").body)

    assert folded("1") == 1
    assert folded("'yes'") == "yes"
    assert folded("bool(1)") is True
    assert folded("2 > 1") is True
    assert folded("not False") is True
    assert folded("True or IS_WINDOWS") is True
    assert folded("False and IS_WINDOWS") is False
    assert folded("IS_WINDOWS or True") is _UNRESOLVED
    assert folded("False") is False
    assert folded("1 > 2") is False
    assert folded("IS_WINDOWS") is _UNRESOLVED
    assert folded("sys.platform == 'win32'") is _UNRESOLVED


def test_the_conditional_definition_rule_fails_on_a_planted_module(tmp_path: Path) -> None:
    # Pins the ASSERTION, not just the helper behind it. No file in the real suite defines a
    # test under a platform condition, so the rule never fires on the tree and neutering it
    # changed nothing measurable -- it survived a mutation while its two helper probes passed.
    # A rule whose only evidence is a helper test is a rule nobody has run.
    module = tmp_path / "test_planted_conditional.py"
    module.write_text(
        "import sys\n\n"
        'if sys.platform == "win32":\n'
        "    def test_windows_only() -> None:\n"
        "        assert True\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="platform condition"):
        test_no_test_is_defined_only_on_one_family(module)


def test_the_conditional_definition_rule_passes_a_clean_module(tmp_path: Path) -> None:
    module = tmp_path / "test_planted_plain.py"
    module.write_text(
        "import sys\n\n\n"
        "def test_thing() -> None:\n"
        '    assert sys.platform is not None\n',
        encoding="utf-8",
    )
    test_no_test_is_defined_only_on_one_family(module)


ALWAYS_TRUE_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("empty string", 'sys.platform != ""', 'sys.platform == ""'),
    ("invented value", 'sys.platform != "grafx-os"', 'sys.platform == "grafx-os"'),
    ("machine rather than family", 'platform.machine() != ""', 'platform.machine() == ""'),
)


@pytest.mark.parametrize(
    ("label", "runs", "never_runs"),
    ALWAYS_TRUE_PAIRS,
    ids=[row[0] for row in ALWAYS_TRUE_PAIRS],
)
def test_a_pair_that_names_no_family_is_not_a_counterpart(
    label: str, runs: str, never_runs: str
) -> None:
    # A89, and the half that could not see it. Accepting any condition that MENTIONS a platform
    # let this module read as correctly paired: the first skips everywhere, the second runs on
    # no family at all, and the important assertion never executes anywhere. The dynamic half
    # refuses the skip, but that masks the static laxity rather than fixing it -- so the closed
    # set is pinned here too, on the half whose whole job is the question runtime cannot answer.
    source = (
        "@pytest.mark.platform_specific\n"
        f'@pytest.mark.skipif({runs}, reason="one family")\n'
        "def test_one_side():\n    pass\n\n\n"
        "@pytest.mark.platform_specific\n"
        f'@pytest.mark.skipif({never_runs}, reason="the other")\n'
        "def test_the_important_one():\n    assert 1 + 1 == 3\n"
    )
    report = _probe(source)
    assert report.tests, "the probe declared no platform_specific tests"
    assert all(test.conditions == () for test in report.tests), (
        "a condition naming no real platform value must not count as declaring a family"
    )
    with pytest.raises(AssertionError, match="none of them runs with a declared"):
        test_every_family_specific_test_has_a_counterpart(report)


def test_the_closed_set_holds_only_real_platform_values() -> None:
    # The set the author cannot extend. Pinned by content so widening it is a failing test
    # rather than a quiet loosening (A56).
    assert "win32" in PLATFORM_VALUES and "nt" in PLATFORM_VALUES
    assert "posix" in PLATFORM_VALUES and "darwin" in PLATFORM_VALUES
    for invented in ("", "grafx-os", "any", "true", "1"):
        assert invented not in PLATFORM_VALUES


REFUSED_SHAPES: tuple[tuple[str, str], ...] = (
    ("or with a constant", "IS_WINDOWS or True"),
    ("constant first", "True or IS_WINDOWS"),
    ("and with a constant", "IS_WINDOWS and True"),
    ("a rescued constant", 'sys.platform == "win32" or HARDCODED_TRUE'),
    ("identity against a bool", "IS_WINDOWS is True"),
    ("negated identity", "IS_WINDOWS is False"),
    ("a call", "bool(IS_WINDOWS)"),
    ("a comparison of two readings", "sys.platform == os.name"),
    ("a truthy reading", "sys.platform"),
)


@pytest.mark.parametrize(
    ("label", "condition"), REFUSED_SHAPES, ids=[row[0] for row in REFUSED_SHAPES]
)
def test_only_the_three_admitted_shapes_name_a_family(label: str, condition: str) -> None:
    """A89 applied to shape: the whole expression is whitelisted, never searched for a part.

    Searching for a matching sub-expression accepted ``IS_WINDOWS or True`` -- true on every
    family, so the test ran on none -- and let ``or HARDCODED_TRUE`` rescue a constant the gate
    refuses on its own. Three shapes are admitted and nothing else, which also retires the
    identity spellings: ``IS_WINDOWS`` and ``not IS_WINDOWS`` already say it, so ``is True`` buys
    nothing and widens the grammar.
    """
    source = (
        "@pytest.mark.platform_specific\n"
        f'@pytest.mark.skipif({condition}, reason="claims a family")\n'
        "def test_the_important_one():\n    raise AssertionError('would fail on any family')\n"
    )
    report = _probe("HARDCODED_TRUE = True\n\n\n" + source)
    assert report.tests, label
    assert report.tests[0].conditions == (), f"{label} passed as a family condition"


ADMITTED_SHAPES: tuple[tuple[str, str], ...] = (
    ("sys.platform equality", 'sys.platform == "win32"'),
    ("sys.platform inequality", 'sys.platform != "win32"'),
    ("os.name equality", 'os.name == "nt"'),
    ("membership in supported values", 'sys.platform in ("win32", "linux")'),
    ("a bound name", "IS_WINDOWS"),
    ("a negated bound name", "not IS_WINDOWS"),
    ("a negated comparison", 'not sys.platform == "win32"'),
)


@pytest.mark.parametrize(
    ("label", "condition"), ADMITTED_SHAPES, ids=[row[0] for row in ADMITTED_SHAPES]
)
def test_the_admitted_shapes_still_name_a_family(label: str, condition: str) -> None:
    source = (
        "@pytest.mark.platform_specific\n"
        f'@pytest.mark.skipif({condition}, reason="one family")\n'
        "def test_one_side():\n    pass\n"
    )
    report = _probe(source)
    assert report.tests[0].conditions != (), label
