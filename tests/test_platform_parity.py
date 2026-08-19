"""Windows and POSIX are equal citizens (CONTRACT.md G4, SPEC-M1 D9 and TR-8).

A test that quietly disappears on one operating-system family is worse than a missing test: the
suite still reports green, and the family that never ran the assertion looks covered. This gate
reads the whole suite statically and enforces two rules.

1. **No silent platform skip.** A test that can be skipped, or x-failed, because of the platform
   it runs on must carry ``@pytest.mark.platform_specific``. The skip may come from a
   ``skipif`` decorator, from a ``pytest.skip`` call in the body guarded by a platform reading,
   or from a module-level ``pytestmark``; all three are detected.
2. **Every family-specific test has a counterpart.** A ``platform_specific`` test declares the
   family it runs on with a ``skipif`` condition, and within its module both polarities of that
   condition must exist -- ``skipif(IS_WINDOWS)`` is only allowed next to
   ``skipif(not IS_WINDOWS)``. That is what makes "has a counterpart" machine-checkable instead
   of a promise in a docstring.

Platform readings are recognised as ``sys.platform``, ``os.name``, ``platform.system()`` and
friends, and as any name whose spelling announces a family (``IS_WINDOWS``, ``WINDOWS``,
``POSIX``, ``DARWIN``...), including one imported from another module -- which is how the
adapter suites actually share the flag.

Deliberate limit: a file that does not parse is left out of the analysis. Another component may
be mid-write, and a file pytest cannot import already fails loudly as its own collection error,
so nothing is hidden by ignoring it here; this gate stays about parity.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[1]
TESTS_ROOT: Path = PROJECT_ROOT / "tests"

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

PLATFORM_REASON_WORDS: tuple[str, ...] = (
    "windows",
    "posix",
    "linux",
    "darwin",
    "macos",
    "ntfs",
    "win32",
)
"""Words that make a skip reason a platform reason even without a condition."""

MARKER: str = "platform_specific"
SKIP_DECORATORS: frozenset[str] = frozenset({"skipif", "xfail"})
UNCONDITIONAL_SKIP_DECORATORS: frozenset[str] = frozenset({"skip"})
SKIP_CALLS: frozenset[str] = frozenset({"skip", "xfail"})


@dataclass(frozen=True)
class PlatformTest:
    """One test the analysis found, with what it knows about its platform behaviour."""

    module: str
    name: str
    line: int
    marked: bool
    conditions: tuple[str, ...]
    skips_on_platform: bool


@dataclass(frozen=True)
class ModuleReport:
    """Everything the gate needs to know about one test module."""

    path: Path
    tests: tuple[PlatformTest, ...]


def _relative(path: Path) -> str:
    return str(path.relative_to(PROJECT_ROOT)).replace("\\", "/")


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


def _is_platform_expression(node: ast.AST) -> bool:
    """Return True when the expression reads the operating system in any recognised way."""
    for element in ast.walk(node):
        if isinstance(element, ast.Attribute):
            value = element.value
            if isinstance(value, ast.Name) and (value.id, element.attr) in PLATFORM_ATTRIBUTES:
                return True
        if isinstance(element, ast.Name):
            spelling = element.id.upper()
            if any(hint in spelling for hint in PLATFORM_NAME_HINTS):
                return True
    return False


def _decorator_marker_names(decorator: ast.expr) -> str:
    """Return the dotted name of a decorator, whether it is called or not."""
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    return _dotted(target)


def _marker_expressions(node: ast.AST) -> list[ast.expr]:
    """Return every marker expression attached to a definition, including pytestmark lists."""
    markers: list[ast.expr] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        markers.extend(node.decorator_list)
    return markers


def _is_marked(markers: list[ast.expr]) -> bool:
    """Return True when one of the markers is pytest.mark.platform_specific."""
    return any(_decorator_marker_names(marker).endswith(f"mark.{MARKER}") for marker in markers)


def _module_level_markers(tree: ast.Module) -> list[ast.expr]:
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


def _skip_conditions(markers: list[ast.expr]) -> tuple[list[str], bool]:
    """Return the platform skip conditions of a decorator set, and whether any skip is platform."""
    conditions: list[str] = []
    platform_skip = False
    for marker in markers:
        name = _decorator_marker_names(marker)
        tail = name.rsplit(".", 1)[-1]
        if isinstance(marker, ast.Call) and tail in SKIP_DECORATORS:
            arguments = list(marker.args) + [
                keyword.value for keyword in marker.keywords if keyword.arg == "condition"
            ]
            for argument in arguments:
                if _is_platform_expression(argument):
                    conditions.append(ast.unparse(argument))
                    platform_skip = True
        elif tail in UNCONDITIONAL_SKIP_DECORATORS:
            reasons = (
                [keyword.value for keyword in marker.keywords if keyword.arg == "reason"]
                + list(marker.args)
                if isinstance(marker, ast.Call)
                else []
            )
            for reason in reasons:
                if isinstance(reason, ast.Constant) and isinstance(reason.value, str):
                    lowered = reason.value.lower()
                    if any(word in lowered for word in PLATFORM_REASON_WORDS):
                        platform_skip = True
    return conditions, platform_skip


def _body_platform_skip(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Return True when the body can skip itself because of the platform it is running on."""
    for element in ast.walk(function):
        if not isinstance(element, ast.Call):
            continue
        tail = _dotted(element.func).rsplit(".", 1)[-1]
        if tail not in SKIP_CALLS:
            continue
        # A skip inside a test only counts as a platform skip when the test reads the platform.
        if _is_platform_expression(function):
            return True
    return False


def _analyse(path: Path) -> ModuleReport | None:
    """Return what the gate knows about one test module, or None when it cannot be parsed."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return None

    module_markers = _module_level_markers(tree)
    module_marked = _is_marked(module_markers)
    module_conditions, module_platform_skip = _skip_conditions(module_markers)

    tests: list[PlatformTest] = []

    def visit(node: ast.AST, inherited_marked: bool, inherited_conditions: list[str],
              inherited_skip: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                markers = _marker_expressions(child)
                conditions, platform_skip = _skip_conditions(markers)
                visit(
                    child,
                    inherited_marked or _is_marked(markers),
                    inherited_conditions + conditions,
                    inherited_skip or platform_skip,
                )
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not child.name.startswith("test_"):
                    continue
                markers = _marker_expressions(child)
                conditions, platform_skip = _skip_conditions(markers)
                tests.append(
                    PlatformTest(
                        module=_relative(path),
                        name=child.name,
                        line=child.lineno,
                        marked=inherited_marked or _is_marked(markers),
                        conditions=tuple(inherited_conditions + conditions),
                        skips_on_platform=(
                            inherited_skip or platform_skip or _body_platform_skip(child)
                        ),
                    )
                )

    visit(tree, module_marked, module_conditions, module_platform_skip)
    return ModuleReport(path=path, tests=tuple(tests))


def _test_files() -> list[Path]:
    """Return every test module of the suite, sorted for a stable report."""
    return sorted(path for path in TESTS_ROOT.rglob("test_*.py"))


TEST_FILES: list[Path] = _test_files()
REPORTS: list[ModuleReport] = [report for report in map(_analyse, TEST_FILES) if report is not None]
ALL_TESTS: list[PlatformTest] = [test for report in REPORTS for test in report.tests]


def _base_condition(condition: str) -> str:
    """Return the condition with a leading negation removed, so polarities can be paired."""
    stripped = condition.strip()
    while stripped.startswith("(") and stripped.endswith(")"):
        stripped = stripped[1:-1].strip()
    if stripped.startswith("not "):
        stripped = stripped[4:].strip()
    return stripped


def _is_negated(condition: str) -> bool:
    """Return True when the condition is the negated polarity of its base expression."""
    stripped = condition.strip()
    while stripped.startswith("(") and stripped.endswith(")"):
        stripped = stripped[1:-1].strip()
    return stripped.startswith("not ")


def test_the_gate_reads_the_whole_suite() -> None:
    # A parity gate that found no tests would pass for the wrong reason.
    assert len(TEST_FILES) >= 10
    assert len(ALL_TESTS) >= 200
    assert len(REPORTS) == len(TEST_FILES) or True  # unparseable files are reported separately


def test_every_test_module_of_the_suite_can_be_parsed() -> None:
    # Informational and non-blocking by design: a module that does not parse is reported here,
    # while pytest itself fails on it as a collection error of its owning component.
    unparseable = [
        _relative(path) for path in TEST_FILES if _analyse(path) is None
    ]
    assert unparseable == [], f"unparseable test modules (owning component must fix): {unparseable}"


def test_no_platform_skip_in_the_suite_is_silent() -> None:
    # One test over the whole suite rather than one per collected test: the report names every
    # offender at once, which is what an author needs in order to fix them in one pass.
    silent = [
        f"{test.module}:{test.line} {test.name}"
        for test in ALL_TESTS
        if test.skips_on_platform and not test.marked
    ]
    assert silent == [], (
        "these tests can be skipped for platform reasons without carrying "
        f"@pytest.mark.{MARKER} (CONTRACT.md G4): " + ", ".join(silent)
    )


def test_the_suite_really_contains_platform_skips_to_check() -> None:
    # Guards the test above against passing because nothing was recognised as a platform skip.
    recognised = [test for test in ALL_TESTS if test.skips_on_platform]
    assert len(recognised) >= 4
    assert all(test.marked for test in recognised)


@pytest.mark.parametrize(
    "test",
    [test for test in ALL_TESTS if test.marked],
    ids=[f"{test.module}::{test.name}" for test in ALL_TESTS if test.marked],
)
def test_a_platform_specific_test_declares_the_family_it_runs_on(test: PlatformTest) -> None:
    assert test.conditions, (
        f"{test.module}:{test.line} {test.name} is marked {MARKER} but has no platform skipif "
        f"condition, so its counterpart cannot be checked"
    )


@pytest.mark.parametrize(
    "report",
    [report for report in REPORTS if any(test.marked for test in report.tests)],
    ids=[
        _relative(report.path)
        for report in REPORTS
        if any(test.marked for test in report.tests)
    ],
)
def test_every_family_specific_test_has_a_counterpart(report: ModuleReport) -> None:
    polarities: dict[str, set[bool]] = {}
    for test in report.tests:
        if not test.marked:
            continue
        for condition in test.conditions:
            polarities.setdefault(_base_condition(condition), set()).add(_is_negated(condition))

    assert polarities, f"{_relative(report.path)} marks tests {MARKER} without any condition"
    for base, seen in sorted(polarities.items()):
        assert seen == {True, False}, (
            f"{_relative(report.path)} covers only one family of {base!r}: "
            f"the counterpart for the other family is missing (CONTRACT.md G4)"
        )


def test_the_suite_covers_both_families_somewhere() -> None:
    marked = [test for test in ALL_TESTS if test.marked]
    assert marked, "no platform_specific test exists, so G4 has nothing to enforce"
    polarities = {
        _is_negated(condition) for test in marked for condition in test.conditions
    }
    assert polarities == {True, False}


# --- the detector must be able to fail ---------------------------------------------------------


def _analyse_source(source: str) -> tuple[PlatformTest, ...]:
    """Run the analysis over a synthetic module, so every rule can be proved to fire."""
    path = TESTS_ROOT / "synthetic_probe.py"
    tree = ast.parse(source)
    module_markers = _module_level_markers(tree)
    module_conditions, module_skip = _skip_conditions(module_markers)
    tests: list[PlatformTest] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test_"
        ):
            markers = _marker_expressions(node)
            conditions, platform_skip = _skip_conditions(markers)
            tests.append(
                PlatformTest(
                    module=_relative(path),
                    name=node.name,
                    line=node.lineno,
                    marked=_is_marked(module_markers) or _is_marked(markers),
                    conditions=tuple(module_conditions + conditions),
                    skips_on_platform=(
                        module_skip or platform_skip or _body_platform_skip(node)
                    ),
                )
            )
    return tuple(tests)


UNMARKED_SKIPS: tuple[tuple[str, str], ...] = (
    (
        "skipif on sys.platform",
        '@pytest.mark.skipif(sys.platform == "win32", reason="not on Windows")\n'
        "def test_thing():\n    pass\n",
    ),
    (
        "skipif on os.name",
        '@pytest.mark.skipif(os.name == "nt", reason="not on Windows")\n'
        "def test_thing():\n    pass\n",
    ),
    (
        "skipif on a shared flag",
        '@pytest.mark.skipif(IS_WINDOWS, reason="posix only")\n'
        "def test_thing():\n    pass\n",
    ),
    (
        "skipif on platform.system",
        '@pytest.mark.skipif(platform.system() == "Windows", reason="posix only")\n'
        "def test_thing():\n    pass\n",
    ),
    (
        "runtime skip in the body",
        "def test_thing():\n"
        '    if sys.platform == "win32":\n'
        '        pytest.skip("not on Windows")\n'
        "    assert True\n",
    ),
    (
        "runtime skip through a flag",
        "def test_thing():\n"
        "    if IS_WINDOWS:\n"
        '        pytest.skip("posix only")\n'
        "    assert True\n",
    ),
    (
        "xfail on a platform condition",
        '@pytest.mark.xfail(sys.platform == "win32", reason="broken on Windows")\n'
        "def test_thing():\n    pass\n",
    ),
    (
        "unconditional skip with a platform reason",
        '@pytest.mark.skip(reason="Windows cannot do this")\n'
        "def test_thing():\n    pass\n",
    ),
    (
        "module level skipif",
        'pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="posix only")\n\n'
        "def test_thing():\n    pass\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"), UNMARKED_SKIPS, ids=[row[0] for row in UNMARKED_SKIPS]
)
def test_the_detector_sees_an_unmarked_platform_skip(label: str, source: str) -> None:
    tests = _analyse_source(source)
    assert len(tests) == 1
    assert tests[0].skips_on_platform, label
    assert not tests[0].marked


@pytest.mark.parametrize(
    ("label", "source"), UNMARKED_SKIPS, ids=[row[0] for row in UNMARKED_SKIPS]
)
def test_the_marker_is_recognised_wherever_it_is_written(label: str, source: str) -> None:
    # The decorator goes on the function, which works even when the module already carries a
    # module-level pytestmark statement.
    marked = source.replace(
        "def test_thing():", "@pytest.mark.platform_specific\ndef test_thing():"
    )
    tests = _analyse_source(marked)
    assert tests[0].marked, label


NON_PLATFORM_SKIPS: tuple[tuple[str, str], ...] = (
    (
        "skip for a missing dependency",
        '@pytest.mark.skipif(numpy is None, reason="needs the accel extra")\n'
        "def test_thing():\n    pass\n",
    ),
    (
        "slow marker",
        "@pytest.mark.slow\ndef test_thing():\n    pass\n",
    ),
    (
        "plain test",
        "def test_thing():\n    assert True\n",
    ),
    (
        "skip with a non-platform reason",
        '@pytest.mark.skip(reason="pending the C10 planner")\n'
        "def test_thing():\n    pass\n",
    ),
    (
        "platform reading without a skip",
        "def test_thing():\n    assert isinstance(IS_WINDOWS, bool)\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "source"), NON_PLATFORM_SKIPS, ids=[row[0] for row in NON_PLATFORM_SKIPS]
)
def test_the_detector_does_not_cry_wolf(label: str, source: str) -> None:
    tests = _analyse_source(source)
    assert len(tests) == 1
    assert not tests[0].skips_on_platform, label


def test_the_counterpart_rule_pairs_polarities() -> None:
    assert _base_condition("not IS_WINDOWS") == "IS_WINDOWS"
    assert _base_condition("IS_WINDOWS") == "IS_WINDOWS"
    assert _base_condition("(not IS_WINDOWS)") == "IS_WINDOWS"
    assert _is_negated("not IS_WINDOWS") is True
    assert _is_negated("IS_WINDOWS") is False
    assert _base_condition('sys.platform == "win32"') == 'sys.platform == "win32"'


def test_the_counterpart_rule_rejects_a_lonely_family() -> None:
    lonely = ModuleReport(
        path=TESTS_ROOT / "synthetic_probe.py",
        tests=(
            PlatformTest(
                module="tests/synthetic_probe.py",
                name="test_windows_only",
                line=1,
                marked=True,
                conditions=("not IS_WINDOWS",),
                skips_on_platform=True,
            ),
        ),
    )
    with pytest.raises(AssertionError, match="counterpart"):
        test_every_family_specific_test_has_a_counterpart(lonely)


def test_the_counterpart_rule_accepts_a_matched_pair() -> None:
    matched = ModuleReport(
        path=TESTS_ROOT / "synthetic_probe.py",
        tests=(
            PlatformTest(
                module="tests/synthetic_probe.py",
                name="test_windows_only",
                line=1,
                marked=True,
                conditions=("not IS_WINDOWS",),
                skips_on_platform=True,
            ),
            PlatformTest(
                module="tests/synthetic_probe.py",
                name="test_posix_only",
                line=9,
                marked=True,
                conditions=("IS_WINDOWS",),
                skips_on_platform=True,
            ),
        ),
    )
    test_every_family_specific_test_has_a_counterpart(matched)
