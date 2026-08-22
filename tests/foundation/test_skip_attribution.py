"""The dynamic half of G4 must be able to fail (CONTRACT.md A32, A35, A36, and B2 of the audit).

The static gates each carry an anti-vacuity suite; the *authoritative* half shipped without one,
and an audit proved the consequence: neutering ``_record_skip`` or ``pytest_sessionfinish`` left
the whole suite green. A detector nothing tests is a detector nobody can trust.

So this file runs the real hook, in a real pytest process, over planted modules:

* every disguised skip reason the audit walked through must now fail the run;
* the three attributions that are genuine -- a source-visible ``platform_specific``, a registered
  ``pending`` marker, a module that really is absent -- must still pass;
* a module removed from collection with no report at all must fail (A36);
* and the detector itself is mutated to confirm each planted case goes green again, which is the
  only way to know the failures above are caused by the check rather than by something else.

Each case is a subprocess so the hook runs exactly as it does in production, session exit status
included. That costs about a second per case, which is the right trade for the one gate whose
correctness every other component's coverage depends on.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
CONFTEST: Path = PROJECT_ROOT / "tests" / "conftest.py"
SOURCE_ROOT: Path = PROJECT_ROOT / "src"

PYPROJECT = """
[project]
name = "planted"
version = "0.0.0"

[project.optional-dependencies]
accel = ["numpy>=1.24"]
bench = ["ladybug==0.16.0", "numpy>=1.24"]
dev = ["pytest>=8", "pytest-timeout"]
probe = ["grafx-absent-probe>=1.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q --strict-markers"
markers = [
    "platform_specific: family specific test.",
    "slow: slow test.",
    "multiprocess: multi-process test.",
    "bench: benchmark test.",
    "pending: declared pending item.",
    "optional_dependency(module): declared optional dependency.",
]
"""

HEADER = '"""Planted."""\n\nfrom __future__ import annotations\n\nimport os\nimport sys\n\nimport pytest\n\n__all__: list[str] = []\n\n\n'


def _find_spec_safe(name: str) -> object | None:
    """Return the import spec of a module, or None when it cannot be imported here."""
    try:
        return importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None


def _plant(
    tmp_path: Path,
    *,
    module: str = "",
    modules: dict[str, str] | None = None,
    conftest_extra: str = "",
    manifest_extra: str = "",
    nested_conftest: str = "",
    nested_module: str = "",
    mutation: tuple[str, str] | None = None,
    extra_arguments: tuple[str, ...] = (),
) -> tuple[int, str]:
    """Build a miniature project around the real hook and run pytest over it."""
    tests = tmp_path / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text(
        PYPROJECT if not manifest_extra else PYPROJECT.replace(
            "[tool.pytest.ini_options]", manifest_extra + "\n[tool.pytest.ini_options]"
        ),
        encoding="utf-8",
    )

    hook = CONFTEST.read_text(encoding="utf-8")
    _refuse_shadowing(hook, conftest_extra)
    if mutation is not None:
        old, new = mutation
        assert old in hook, f"mutation target not found: {old!r}"
        hook = hook.replace(old, new, 1)
    (tests / "conftest.py").write_text(hook + conftest_extra, encoding="utf-8")

    if nested_conftest or nested_module:
        # A hook appended to the copied conftest REPLACES the one under test, because both end
        # up in one module. A nested package keeps the planted hook in a file of its own, which
        # is how a real conftest would reach the session anyway.
        nested = tests / "nested"
        nested.mkdir(exist_ok=True)
        (nested / "conftest.py").write_text(
            HEADER + nested_conftest, encoding="utf-8"
        )
        (nested / "test_nested.py").write_text(
            nested_module or (HEADER + "def test_planted() -> None:\n    assert True\n"),
            encoding="utf-8",
        )
    if module:
        (tests / "test_planted.py").write_text(module, encoding="utf-8")
    for name, text in (modules or {}).items():
        (tests / name).write_text(text, encoding="utf-8")
    # Every planted project collects at least one ordinary passing test. Without it, a project
    # whose only module vanishes exits 5 (no tests collected), and a probe asserting "non-zero"
    # would pass on pytest's own bookkeeping rather than on the detector -- a confound a
    # mutation re-run caught surviving two mutants.
    (tests / "test_baseline.py").write_text(
        HEADER + "def test_baseline() -> None:\n    assert True\n", encoding="utf-8"
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q"]
        # --lf, --ff and --sw come from the cache plugin, so a probe exercising them must
        # not switch it off; in a throwaway project the cache is noise either way.
        + ([] if extra_arguments else ["-p", "no:cacheprovider"])
        + list(extra_arguments),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **_clean_environment(),
            "PYTHONPATH": str(SOURCE_ROOT),
            "PYTHONDONTWRITEBYTECODE": "1",
            # Autoloading the interpreter's third-party plugins costs eleven seconds per run and
            # buys nothing here: the planted project needs pytest and this hook, nothing else.
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            # The planted projects are three files; the AST floor covers them, so they do
            # not need the reference collection and should not pay for a second pytest.
            "OKTO_G4_REFERENCE": "1",
        },
    )
    return result.returncode, result.stdout + result.stderr


def _hook_names(source: str) -> set[str]:
    """Return the pytest hook functions a source text defines."""
    return {
        node.name
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("pytest_")
    }


def _refuse_shadowing(hook_source: str, extra: str) -> None:
    """Refuse an extra that would silently replace a hook of the conftest under test.

    Three probes in this file have passed for the wrong reason this way: appending a second
    ``pytest_collection_modifyitems`` (or ``pytest_collection_finish``) puts two functions of one
    name in one module, and the later definition wins. The probe then measures whichever rule
    happens to be left. Caught here rather than discovered a fourth time.
    """
    if not extra.strip():
        return
    clashes = _hook_names(hook_source) & _hook_names(extra)
    assert not clashes, (
        f"the planted extra would shadow {sorted(clashes)} in the conftest under test; "
        f"use nested_conftest= so the hook lives in its own module"
    )


def _clean_environment() -> dict[str, str]:
    """Return the environment without a PYTHONPATH that would shadow the planted project."""
    import os

    return {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}


def _skipping_module(reason: str, *, decorators: str = "") -> str:
    """Return a module whose test skips unconditionally, on every operating-system family.

    Unconditional on purpose. Conditioning the planted skip on win32 made every probe here pass
    vacuously on POSIX -- including all three detector mutants -- so the authoritative half of
    G4 had no anti-vacuity evidence on the family it was not developed on. A planted probe is a
    fixture, not a platform-specific test, and a fixture runs everywhere.
    """
    return (
        HEADER
        + decorators
        + "def test_planted() -> None:\n"
        + f"    pytest.skip({reason!r})\n"
    )


# --- the disguised reasons the audit walked through -------------------------------------------

DISGUISED_REASONS: tuple[str, ...] = (
    "pending Windows support for the POSIX flock path",
    "pending: only runs on Linux",
    "could not import the windows-only lock helper",
    "this test is not installed on windows",
    "requires the [win] extra",
    "COULD NOT IMPORT anything",
    "pending POSIX support",
    "pending posix support",
    "optional dependency missing",
    "no reason at all",
    # A54: a fabricated module name is absent everywhere, so an absence check answers honestly
    # and the prose still buys nothing. This is the shape that vanished four modules.
    "could not import 'grafx_win_helper': No module named 'grafx_win_helper'",
    "could not import 'readline': No module named 'readline'",
)


def test_every_disguised_skip_reason_fails_the_run(tmp_path: Path) -> None:
    # One process, one module per reason: a subprocess costs a second and the verdict is the
    # same for all of them, so batching keeps the suite affordable without losing a case.
    modules = {
        f"test_disguise_{index}.py": _skipping_module(reason)
        for index, reason in enumerate(DISGUISED_REASONS)
    }
    code, output = _plant(tmp_path, modules=modules)
    assert code != 0, f"the disguised reasons walked through:\n{output}"
    assert "unattributed skips" in output
    for index, reason in enumerate(DISGUISED_REASONS):
        assert f"test_disguise_{index}" in output, f"{reason!r} was not named"
        assert reason in output, f"{reason!r} was not reported"


def test_the_offender_and_its_reason_are_named(tmp_path: Path) -> None:
    code, output = _plant(tmp_path, module=_skipping_module("pending Windows support"))
    assert code != 0
    assert "tests/test_planted.py::test_planted" in output.replace("\\", "/")
    assert "pending Windows support" in output


def test_modules_of_failing_assertions_cannot_be_vanished(tmp_path: Path) -> None:
    # The audit's scale demonstration: bodies that would fail if they ran, hidden behind a
    # module-level skip with a plausible reason, reported as a clean pass.
    module = (
        HEADER
        + 'pytest.skip("pending Windows support for the POSIX path", allow_module_level=True)\n\n\n'
        + 'def test_would_fail() -> None:\n    assert 1 + 1 == 3, "this would FAIL if it ran"\n'
    )
    code, output = _plant(tmp_path, module=module)
    # Exactly 1: the baseline test passes, so pytest would exit 0 on its own and only the
    # detector can make this run fail.
    assert code == 1, output
    # The skip-specific sentence, not just the section header. A36's disappearance rule also
    # covers a module-level skip -- welcome defence in depth, but it meant this probe passed
    # with the skip recorder neutered, so it has to name the rule it is actually testing.
    assert "skipped test(s) carry neither" in output, output
    assert "would FAIL if it ran" not in output


# --- the attributions that are genuine ---------------------------------------------------------


def test_a_source_visible_platform_marker_attributes_the_skip(tmp_path: Path) -> None:
    # The mark AND the family condition: both halves of G4 now ask for the same evidence,
    # so a test that declares which family it runs on is attributed here and its counterpart
    # is demanded by the static half. A mark on its own names nothing and pays nothing.
    module = _skipping_module(
        "posix only",
        decorators=(
            '@pytest.mark.skipif(sys.platform == "win32", reason="windows only")\n'
            "@pytest.mark.platform_specific\n"
        ),
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 0, output
    assert "unattributed skips" not in output


def test_a_registered_pending_marker_attributes_the_skip(tmp_path: Path) -> None:
    module = _skipping_module("waiting for C10", decorators="@pytest.mark.pending\n")
    code, output = _plant(tmp_path, module=module)
    assert code == 0, output


UNDECLARED_DEPENDENCY_NAMES: tuple[str, ...] = (
    # The name A54.1 was written for: absent everywhere, so find_spec answers honestly, and
    # worthless because the author chose it.
    "grafx_win_helper",
    # Real single-family stdlib modules the denylist never enumerated, and never could.
    "_posixshmem",
    "_scproxy",
    "_gdbm",
    "_posixsubprocess",
    # A submodule of something that IS installed, which find_spec still calls absent.
    "json.absolutely_not_here",
    # Absent, plausible, and not in the manifest.
    "definitely_not_installed_xyz",
)


@pytest.mark.parametrize("name", UNDECLARED_DEPENDENCY_NAMES)
def test_an_undeclared_distribution_never_attributes(name: str, tmp_path: Path) -> None:
    # A54.1: absence is necessary and not sufficient. Every one of these is genuinely absent and
    # every one is refused, because none is declared in the manifest of the project under test.
    module = (
        HEADER
        + f'@pytest.mark.optional_dependency("{name}")\n'
        + "def test_planted() -> None:\n"
        + '    pytest.skip("needs the helper")\n'
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


def test_a_module_level_undeclared_dependency_cannot_vanish_a_module(tmp_path: Path) -> None:
    # A54.1 headline: the fabricated name moved from the reason string into the marker argument,
    # vanishing four modules of failing assertions. It is refused at both levels now.
    module = (
        '"""Planted."""\n\nfrom __future__ import annotations\n\nimport pytest\n\n'
        + "__all__: list[str] = []\n\n"
        + 'pytestmark = pytest.mark.optional_dependency("grafx_win_helper")\n\n'
        + 'pytest.skip("needs the windows helper", allow_module_level=True)\n\n\n'
        + 'def test_would_fail() -> None:\n    assert 1 + 1 == 3, "this would FAIL if it ran"\n'
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output


def test_a_declared_dependency_that_is_installed_never_attributes(tmp_path: Path) -> None:
    # numpy is declared under [accel] in the planted manifest and is installed here, so the
    # claim is checkable and false: an installed dependency cannot excuse a skip.
    module = (
        HEADER
        + '@pytest.mark.optional_dependency("numpy")\n'
        + "def test_planted() -> None:\n"
        + '    pytest.skip("needs numpy")\n'
    )
    code, output = _plant(tmp_path, module=module)
    if _find_spec_safe("numpy") is None:
        assert code == 0, output
        return
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


def test_an_undeclared_absent_dependency_no_longer_attributes(tmp_path: Path) -> None:
    # A54 withdrew the inference: importorskip alone says nothing a fabricated module name
    # could not also say. The module must DECLARE what it needs, and the gate checks it.
    module = (
        HEADER
        + "def test_planted() -> None:\n"
        + '    pytest.importorskip("definitely_not_installed_xyz")\n'
        + "    assert True\n"
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


def test_a_passing_suite_stays_green(tmp_path: Path) -> None:
    module = HEADER + "def test_planted() -> None:\n    assert True\n"
    code, output = _plant(tmp_path, module=module)
    assert code == 0, output


# --- evidence, not prose ------------------------------------------------------------------------


PLATFORM_ONLY_SPELLINGS: tuple[str, ...] = ("fcntl", "FCNTL", "msvcrt", "MSVCRT", "termios")


def test_a_platform_only_module_never_buys_an_attribution(tmp_path: Path) -> None:
    # The case-sensitivity asymmetry A35 calls out: the platform check must not care about case
    # while the reason match does. Whichever of these is absent on this family, its skip must be
    # refused; the ones that import produce no skip and are silent by construction.
    modules = {
        f"test_only_{index}.py": (
            HEADER
            + "def test_planted() -> None:\n"
            + f'    pytest.importorskip("{name}")\n'
            + "    assert True\n"
        )
        for index, name in enumerate(PLATFORM_ONLY_SPELLINGS)
    }
    # Whether a skip happens is decided HERE, from this interpreter, not read out of the child's
    # own report: asking the detector's output whether the detector should have fired is circular,
    # and a mutation re-run found this probe surviving a neutered recorder because of it.
    absent = [
        name for name in PLATFORM_ONLY_SPELLINGS if _find_spec_safe(name) is None
    ]
    code, output = _plant(tmp_path, modules=modules)
    if not absent:
        assert code == 0, output
        return
    assert code != 0, output
    assert "unattributed skips" in output
    assert "could not import" in output


def test_a_reason_naming_an_installed_module_is_not_a_dependency_skip(tmp_path: Path) -> None:
    # "could not import json" is a lie: json is right there. Evidence beats prose.
    code, output = _plant(tmp_path, module=_skipping_module("could not import json"))
    assert code != 0, output


def test_a_marker_added_at_collection_time_does_not_attribute(tmp_path: Path) -> None:
    # A35/A54: the dynamic half would see it, the AST-only static half never would, so the
    # counterpart price would never be paid. The hook lives in a nested conftest because the
    # capture hook under test is now pytest_itemcollected too, and a second definition of one
    # name in one module silently replaces the first.
    nested_conftest = (
        "\n\ndef pytest_itemcollected(item):\n"
        "    item.add_marker(pytest.mark.platform_specific)\n"
    )
    nested_module = (
        HEADER + "def test_planted() -> None:\n    pytest.skip('posix only')\n"
    )
    code, output = _plant(
        tmp_path, nested_conftest=nested_conftest, nested_module=nested_module
    )
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


# --- disappearance, not merely skipping (A36) --------------------------------------------------


def test_collect_ignore_cannot_remove_a_module(tmp_path: Path) -> None:
    conftest_extra = '\n\ncollect_ignore = ["test_planted.py"]\n'
    module = HEADER + "def test_planted() -> None:\n    assert True\n"
    code, output = _plant(tmp_path, module=module, conftest_extra=conftest_extra)
    assert code != 0, output
    assert "vanished" in output


def test_an_ignore_collect_hook_cannot_remove_a_module(tmp_path: Path) -> None:
    conftest_extra = (
        "\n\ndef pytest_ignore_collect(collection_path, config):\n"
        "    if collection_path.name == 'test_planted.py':\n"
        "        return True\n"
        "    return None\n"
    )
    module = HEADER + "def test_planted() -> None:\n    assert True\n"
    code, output = _plant(tmp_path, module=module, conftest_extra=conftest_extra)
    assert code != 0, output
    assert "vanished" in output


def test_a_conftest_deselection_cannot_remove_a_module(tmp_path: Path) -> None:
    # The hook lives in a nested conftest, so it does not replace the one under test. That
    # confound made this probe pass through the disappearance rule instead of the deselection
    # rule; the shadowing guard in _plant now refuses the shape outright.
    nested_conftest = (
        "\n\ndef pytest_collection_finish(session):\n"
        "    removed = [i for i in session.items if 'nested' in i.nodeid]\n"
        "    if removed:\n"
        "        session.config.hook.pytest_deselected(items=removed)\n"
        "        session.items[:] = [i for i in session.items if i not in removed]\n"
    )
    code, output = _plant(tmp_path, nested_conftest=nested_conftest)
    assert code == 1, output


# --- the detector itself must be load-bearing ---------------------------------------------------

MUTATIONS: tuple[tuple[str, tuple[str, str]], ...] = (
    (
        "record_skip neutered",
        (
            '    """Record a skip unless a source-visible registered marker attributes it."""',
            '    """Record a skip unless a source-visible registered marker attributes it."""\n    return',
        ),
    ),
    (
        "sessionfinish neutered",
        (
            '    """Fail the session when a skip or a disappearance could not be attributed."""',
            '    """Fail the session when a skip or a disappearance could not be attributed."""\n    return',
        ),
    ),
    (
        "attribution always succeeds",
        (
            "    if item is None:\n        return None",
            '    return "mutant"\n    if item is None:\n        return None',
        ),
    ),
)


@pytest.mark.parametrize(
    ("label", "mutation"), MUTATIONS, ids=[row[0] for row in MUTATIONS]
)
def test_neutering_the_detector_lets_the_planted_skip_through(
    label: str, mutation: tuple[str, str], tmp_path: Path
) -> None:
    # If this passes with the mutation applied, the failures above are produced by the detector
    # and not by something incidental. This is the test that makes the others mean something.
    code, output = _plant(
        tmp_path, module=_skipping_module("pending Windows support"), mutation=mutation
    )
    assert code == 0, f"{label}: expected the mutant to let it through, got:\n{output}"


def test_the_unmutated_detector_catches_the_same_case(tmp_path: Path) -> None:
    code, _ = _plant(tmp_path, module=_skipping_module("pending Windows support"))
    assert code != 0


# --- A54: only a source-visible registered marker attributes ----------------------------------


def test_a_fabricated_module_name_buys_nothing(tmp_path: Path) -> None:
    # The audit shape: a name that is absent everywhere, so find_spec confirms "genuinely
    # absent" every time. Under A54 the reason is never read, so the fabrication is inert.
    module = _skipping_module(
        "could not import 'grafx_win_helper': No module named 'grafx_win_helper'"
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


REAL_SINGLE_FAMILY_MODULES: tuple[str, ...] = (
    "_posixsubprocess",
    "readline",
    "_curses",
    "fcntl",
    "termios",
)


def test_importorskip_on_a_real_single_family_module_buys_nothing(tmp_path: Path) -> None:
    # The three the denylist had never enumerated, plus two it had. Under A54 none of them
    # matters: no marker, no attribution, denylist or not.
    absent = [name for name in REAL_SINGLE_FAMILY_MODULES if _find_spec_safe(name) is None]
    modules = {
        f"test_family_{index}.py": (
            HEADER
            + "def test_planted() -> None:\n"
            + f'    pytest.importorskip("{name}")\n'
            + "    assert True\n"
        )
        for index, name in enumerate(REAL_SINGLE_FAMILY_MODULES)
    }
    code, output = _plant(tmp_path, modules=modules)
    if not absent:
        assert code == 0, output
        return
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


def test_a_declared_absent_optional_dependency_attributes(tmp_path: Path) -> None:
    # Both halves of the A54.1 claim: the distribution is declared in the manifest of the
    # project under test AND genuinely absent from this interpreter.
    module = (
        HEADER
        + '@pytest.mark.optional_dependency("grafx_absent_probe")\n'
        + "def test_planted() -> None:\n"
        + '    pytest.importorskip("grafx_absent_probe")\n'
        + "    assert True\n"
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 0, output


def test_a_declared_dependency_that_is_present_does_not_attribute(tmp_path: Path) -> None:
    # Declaring "json" and then skipping is a lie the gate can check: json is right there.
    module = (
        HEADER
        + '@pytest.mark.optional_dependency("json")\n'
        + "def test_planted() -> None:\n"
        + '    pytest.skip("posix only")\n'
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


def test_a_declared_single_family_module_never_qualifies(tmp_path: Path) -> None:
    # A54: a module in PLATFORM_ONLY_MODULES is never an optional dependency, so declaring one
    # is the platform escape wearing the dependency marker.
    module = (
        HEADER
        + '@pytest.mark.optional_dependency("fcntl")\n'
        + "def test_planted() -> None:\n"
        + '    pytest.importorskip("fcntl")\n'
        + "    assert True\n"
    )
    code, output = _plant(tmp_path, module=module)
    if _find_spec_safe("fcntl") is not None:
        assert code == 0, output
        return
    assert code == 1, output


def test_a_module_level_optional_dependency_is_declared_absent_not_vanished(
    tmp_path: Path,
) -> None:
    # SPEC-VEC TR-6 requires a CI run WITHOUT the accel extra, so the module-level importorskip
    # idiom has to stay possible. Paired with the marker, the module is declared-absent.
    module = (
        '"""Planted."""\n\nfrom __future__ import annotations\n\nimport pytest\n\n'
        + "__all__: list[str] = []\n\n"
        + 'pytestmark = pytest.mark.optional_dependency("grafx_absent_probe")\n\n'
        + 'accelerator = pytest.importorskip("grafx_absent_probe")\n\n\n'
        + "def test_planted() -> None:\n    assert True\n"
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 0, output
    assert "vanished" not in output


def test_a_bare_skip_with_no_reason_is_refused(tmp_path: Path) -> None:
    # The surviving mutation: an empty reason used to satisfy the attribution check.
    module = HEADER + "def test_planted() -> None:\n    pytest.skip()\n"
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


def test_a_decorator_that_merely_shares_a_marker_name_attributes_nothing(
    tmp_path: Path,
) -> None:
    # Nothing is registered and --strict-markers never sees it, so the spelling must not count.
    module = (
        HEADER
        + "def pending(function):\n    return function\n\n\n"
        + "@pending\n"
        + "def test_planted() -> None:\n"
        + '    pytest.skip("waiting")\n'
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


def test_an_unregistered_marker_cannot_attribute(tmp_path: Path) -> None:
    # --strict-markers turns this into a collection error, which is a loud non-zero of its own.
    module = (
        HEADER
        + "@pytest.mark.deferred\n"
        + "def test_planted() -> None:\n"
        + '    pytest.skip("later")\n'
    )
    code, output = _plant(tmp_path, module=module)
    assert code != 0, output


# --- A56: the constants that decide whether the gate exists ------------------------------------

GATE_WIDENING_MUTATIONS: tuple[tuple[str, tuple[str, str]], ...] = (
    (
        "PENDING_MARKERS gains skipif",
        (
            'DEBT_MARKERS: frozenset[str] = frozenset({"pending", "xfail"})',
            'DEBT_MARKERS: frozenset[str] = frozenset({"pending", "xfail", "skipif"})',
        ),
    ),
    (
        "PENDING_MARKERS gains skip",
        (
            'DEBT_MARKERS: frozenset[str] = frozenset({"pending", "xfail"})',
            'DEBT_MARKERS: frozenset[str] = frozenset({"pending", "xfail", "skip"})',
        ),
    ),
    (
        "ATTRIBUTING_MARKERS gains skipif",
        (
            "    frozenset({PLATFORM_MARKER, DEPENDENCY_MARKER}) | DEBT_MARKERS",
            '    frozenset({PLATFORM_MARKER, DEPENDENCY_MARKER, "skipif"}) | DEBT_MARKERS',
        ),
    ),
)


@pytest.mark.parametrize(
    ("label", "mutation"),
    GATE_WIDENING_MUTATIONS,
    ids=[row[0] for row in GATE_WIDENING_MUTATIONS],
)
def test_widening_the_marker_set_is_caught(
    label: str, mutation: tuple[str, str], tmp_path: Path
) -> None:
    # A56: a gate whose enabling constant is unpinned can be switched off in one token. An
    # ordinary skipif test with a failing body must stay refused however the set is widened.
    module = (
        HEADER
        + '@pytest.mark.skipif(sys.platform == "win32", reason="windows only")\n'
        + "def test_planted() -> None:\n"
        + '    assert 1 + 1 == 3, "this would FAIL if it ran"\n'
    )
    code, output = _plant(tmp_path, module=module, mutation=mutation)
    assert code != 0, f"{label} switched the gate off:\n{output}"


def test_the_marker_set_is_exactly_the_three_the_amendment_names() -> None:
    # Read from the shipped conftest rather than imported, so this asserts the file the runs use.
    source = CONFTEST.read_text(encoding="utf-8")
    assert 'DEBT_MARKERS: frozenset[str] = frozenset({"pending", "xfail"})' in source
    assert 'PLATFORM_MARKER: str = "platform_specific"' in source
    assert 'DEPENDENCY_MARKER: str = "optional_dependency"' in source
    assert "frozenset({PLATFORM_MARKER, DEPENDENCY_MARKER}) | DEBT_MARKERS" in source


# --- A55: a filter is not a violation ----------------------------------------------------------

FILTERED_INVOCATIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("keyword", ("-k", "baseline")),
    ("marker expression", ("-m", "not slow")),
    ("last failed", ("--lf",)),
    ("failed first", ("--ff",)),
    ("stepwise", ("--sw",)),
    ("explicit node id", ("tests/test_baseline.py::test_baseline",)),
    ("deselect", ("--deselect", "tests/test_planted.py::test_planted")),
)


@pytest.mark.parametrize(
    ("label", "arguments"), FILTERED_INVOCATIONS, ids=[row[0] for row in FILTERED_INVOCATIONS]
)
def test_an_ordinary_filtered_invocation_stays_green(
    label: str, arguments: tuple[str, ...], tmp_path: Path
) -> None:
    # A55: every test in the tree passes, so every one of these must exit 0. A gate that fails a
    # correct tree trains authors to route around it.
    module = HEADER + "def test_planted() -> None:\n    assert True\n"
    code, output = _plant(tmp_path, module=module, extra_arguments=arguments)
    assert code == 0, f"{label} failed a green tree:\n{output}"
    assert "vanished" not in output
    assert "unattributed" not in output


def test_a_dependency_marker_naming_no_module_attributes_nothing(tmp_path: Path) -> None:
    # A registered marker that takes an argument still accepts a bare use, and a bare use names
    # nothing for the gate to verify -- which would be the password A54 removed, wearing the
    # newest marker. The claim has to name a module before it can be checked.
    module = (
        HEADER
        + "@pytest.mark.optional_dependency\n"
        + "def test_planted() -> None:\n"
        + '    pytest.skip("posix only")\n'
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


def test_a_dependency_marker_with_an_empty_name_attributes_nothing(tmp_path: Path) -> None:
    module = (
        HEADER
        + '@pytest.mark.optional_dependency("")\n'
        + "def test_planted() -> None:\n"
        + '    pytest.skip("posix only")\n'
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output


def test_the_battery_lock_lives_in_the_repository(tmp_path: Path) -> None:
    # A60: shared tooling is in-tree and under version control, so no agent is asked to execute
    # code from outside the repository. Confirmed here rather than taken on trust.
    driver = PROJECT_ROOT / "tools" / "battery_lock.py"
    assert driver.is_file()
    source = driver.read_text(encoding="utf-8")
    assert "def acquire(" in source and "def release(" in source
    assert "BatteryLockUnavailable" in source
    # And it is outside every root the three source gates walk, so it is scanned by none of them
    # and ships in no wheel.
    assert (PROJECT_ROOT / "tools") != (PROJECT_ROOT / "src" / "okto_grafx")
    assert (PROJECT_ROOT / "src" / "okto_grafx") not in driver.parents
    assert (PROJECT_ROOT / "tests") not in driver.parents


def test_a_platform_marker_naming_no_family_attributes_nothing(tmp_path: Path) -> None:
    # A marker is better than prose only because it carries a claim the gate can CHECK. A bare
    # platform_specific names no family, so the static half can demand no counterpart and the
    # mark costs nothing -- the password again, in the newest envelope. Recorded because C0
    # reached for exactly this on its own gate suite and the parity gate caught it.
    module = (
        HEADER
        + "@pytest.mark.platform_specific\n"
        + "def test_planted() -> None:\n"
        + '    pytest.skip("posix only")\n'
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


def test_a_platform_marker_with_a_non_platform_condition_attributes_nothing(
    tmp_path: Path,
) -> None:
    # The exact shape C0 got wrong: an interpreter-version condition wearing the platform mark.
    module = (
        HEADER
        + "import sys as _sys\n\n\n"
        + "@pytest.mark.skipif(_sys.version_info >= (3, 0), reason=\"a version question\")\n"
        + "@pytest.mark.platform_specific\n"
        + "def test_planted() -> None:\n"
        + "    assert True\n"
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output


def test_a_platform_marker_naming_a_family_still_attributes(tmp_path: Path) -> None:
    module = (
        HEADER
        + '@pytest.mark.skipif(sys.platform == "win32", reason="windows only")\n'
        + "@pytest.mark.platform_specific\n"
        + "def test_planted() -> None:\n"
        + "    assert True\n"
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 0, output

# --- A69: the reconciliation backstop, one probe per mechanism the rules did not watch ---------


def test_a_collection_finish_removal_without_deselection_is_caught(tmp_path: Path) -> None:
    # Mechanism 1. The earlier probe passed only because its planted hook politely called
    # pytest_deselected; drop the courtesy and the identical hook walked straight through. The
    # reconciliation does not care how the item left -- it never reported, so it never ran.
    nested_conftest = (
        "\n\ndef pytest_collection_finish(session):\n"
        "    session.items[:] = [i for i in session.items if 'nested' not in i.nodeid]\n"
    )
    code, output = _plant(tmp_path, nested_conftest=nested_conftest)
    assert code == 1, output
    assert "collected but never reported" in output


def test_removing_only_some_items_of_a_module_is_caught(tmp_path: Path) -> None:
    # Mechanism 2. The disappearance rule is per-module, so a module that keeps one test looks
    # present. The reconciliation is per node id.
    nested_conftest = (
        "\n\ndef pytest_collection_finish(session):\n"
        "    session.items[:] = [i for i in session.items if 'vanishes' not in i.nodeid]\n"
    )
    nested_module = (
        HEADER
        + "def test_survives() -> None:\n    assert True\n\n\n"
        + "def test_vanishes() -> None:\n    assert 1 + 1 == 3\n"
    )
    code, output = _plant(
        tmp_path, nested_conftest=nested_conftest, nested_module=nested_module
    )
    assert code == 1, output
    assert "collected but never reported" in output


def test_a_runtest_protocol_that_reports_nothing_is_caught(tmp_path: Path) -> None:
    # Mechanism 3. The item is collected, the protocol claims to have handled it, and no report
    # of any kind is produced -- invisible to every rule that reads reports.
    nested_conftest = (
        "\n\ndef pytest_runtest_protocol(item, nextitem):\n"
        "    if 'nested' in item.nodeid:\n"
        "        return True\n"
        "    return None\n"
    )
    nested_module = HEADER + "def test_planted() -> None:\n    assert 1 + 1 == 3\n"
    code, output = _plant(
        tmp_path, nested_conftest=nested_conftest, nested_module=nested_module
    )
    assert code == 1, output
    assert "collected but never reported" in output


def test_a_collection_time_xfail_marker_is_caught(tmp_path: Path) -> None:
    # Mechanism 4. Enforcing "a collection-time marker does not count" for skip and not for
    # xfail was two paths for one invariant, and the xfail path suppressed a real failure.
    nested_conftest = (
        "\n\ndef pytest_itemcollected(item):\n"
        "    item.add_marker(pytest.mark.xfail(reason='known'))\n"
    )
    nested_module = HEADER + "def test_planted() -> None:\n    assert 1 + 1 == 3\n"
    code, output = _plant(
        tmp_path, nested_conftest=nested_conftest, nested_module=nested_module
    )
    assert code == 1, output


def test_an_imperative_xfail_from_a_setup_hook_is_caught(tmp_path: Path) -> None:
    # Mechanism 5.
    conftest_extra = (
        "\n\ndef pytest_runtest_setup(item):\n"
        "    if 'planted' in item.nodeid:\n"
        "        pytest.xfail('not today')\n"
    )
    module = HEADER + "def test_planted() -> None:\n    assert 1 + 1 == 3\n"
    code, output = _plant(tmp_path, module=module, conftest_extra=conftest_extra)
    assert code == 1, output


def test_a_source_visible_xfail_still_attributes(tmp_path: Path) -> None:
    # The counterpart: a debt declared in the source, which A54 names as attributing.
    module = (
        HEADER
        + "@pytest.mark.xfail(reason='known issue')\n"
        + "def test_planted() -> None:\n    assert 1 + 1 == 3\n"
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 0, output


def test_an_ordinary_run_reconciles_cleanly(tmp_path: Path) -> None:
    # The backstop must not cry wolf: every collected test reported, nothing outstanding.
    module = (
        HEADER
        + "def test_one() -> None:\n    assert True\n\n\n"
        + "def test_two() -> None:\n    assert True\n"
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 0, output
    assert "collected but never reported" not in output


def test_a_failing_test_still_reports(tmp_path: Path) -> None:
    # A failure is a report. The reconciliation must not add noise to an ordinary red run.
    module = HEADER + "def test_planted() -> None:\n    assert 1 + 1 == 3\n"
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output
    assert "collected but never reported" not in output


# --- M3: mutants the suite could not previously tell apart --------------------------------------


def test_a_marker_only_pytest_could_know_about_does_not_attribute(tmp_path: Path) -> None:
    # Pins the clause carrying A54's central claim: the marker must be in the SOURCE. Uses
    # pending rather than platform_specific, because platform_specific is refused by the
    # family-condition rule and so cannot tell the two guards apart (A62).
    nested_conftest = (
        "\n\ndef pytest_itemcollected(item):\n"
        "    item.add_marker(pytest.mark.pending)\n"
    )
    nested_module = (
        HEADER + "def test_planted() -> None:\n    pytest.skip('waiting')\n"
    )
    code, output = _plant(
        tmp_path, nested_conftest=nested_conftest, nested_module=nested_module
    )
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


def test_a_marker_written_for_another_test_of_the_same_name_does_not_attribute(
    tmp_path: Path,
) -> None:
    # Pins the clause the source scan cannot carry alone: markers are read by test NAME, so two
    # classes with the same method name share what the source says. Only the live marker set --
    # which pytest alone populates -- can tell them apart.
    module = (
        HEADER
        + "class TestDeclared:\n"
        + "    @pytest.mark.pending\n"
        + "    def test_thing(self) -> None:\n        assert True\n\n\n"
        + "class TestUndeclared:\n"
        + "    def test_thing(self) -> None:\n"
        + "        pytest.skip('no marker of my own')\n"
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


# --- A89 D1: the closed set binds the WHOLE argument -------------------------------------------

SUBMODULE_OF_A_DECLARED_DISTRIBUTION: tuple[str, ...] = (
    # find_spec answers None for each of these while the root package is installed, so binding
    # the set to the root turned every declared distribution into an unbounded namespace.
    "numpy.absolutely_not_here",
    "numpy.win_helper",
    "pytest.grafx_win_helper",
    "pytest_timeout.helper",
    "ladybug.win32",
)


@pytest.mark.parametrize("name", SUBMODULE_OF_A_DECLARED_DISTRIBUTION)
def test_a_submodule_of_a_declared_distribution_never_attributes(
    name: str, tmp_path: Path
) -> None:
    # A89: the marker names a DISTRIBUTION, and the closed set binds the entire argument. A dot
    # and any suffix is the author extending the set, which is the property A54.1 asked for and
    # a root-only check gave away.
    module = (
        HEADER
        + f'@pytest.mark.optional_dependency("{name}")\n'
        + "def test_planted() -> None:\n"
        + '    pytest.skip("needs the helper")\n'
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


def test_a_module_level_submodule_claim_cannot_vanish_failing_bodies(tmp_path: Path) -> None:
    # The audit's demonstration at module scope: three bodies that would fail, hidden behind a
    # submodule of an installed distribution.
    module = (
        '"""Planted."""\n\nfrom __future__ import annotations\n\nimport pytest\n\n'
        + "__all__: list[str] = []\n\n"
        + 'pytestmark = pytest.mark.optional_dependency("numpy.win_helper")\n\n'
        + 'pytest.skip("needs the numpy helper", allow_module_level=True)\n\n\n'
        + "def test_one() -> None:\n    assert 1 + 1 == 3\n\n\n"
        + "def test_two() -> None:\n    assert 1 + 1 == 3\n\n\n"
        + "def test_three() -> None:\n    assert 1 + 1 == 3\n"
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output


@pytest.mark.parametrize("name", ["numpy", "pytest", "pytest-timeout", "pytest_timeout"])
def test_every_declared_distribution_that_is_installed_is_refused(
    name: str, tmp_path: Path
) -> None:
    # The real closed set, not a probe-only subset: the planted manifest now mirrors the project
    # table, so these exercise the same members the shipped rule would see.
    module = (
        HEADER
        + f'@pytest.mark.optional_dependency("{name}")\n'
        + "def test_planted() -> None:\n"
        + '    pytest.skip("needs it")\n'
    )
    code, output = _plant(tmp_path, module=module)
    root = name.replace("-", "_")
    if _find_spec_safe(root) is None:
        assert code == 0, output
        return
    assert code == 1, output


# --- A89 D3: a family condition names a real platform value ------------------------------------

CONDITIONS_NAMING_NO_FAMILY: tuple[tuple[str, str], ...] = (
    ("always true inequality", 'sys.platform != ""'),
    ("always true membership", 'sys.platform != "definitely-not-an-os"'),
    ("hardcoded flag", "HARDCODED_TRUE"),
    ("comparison to an invented value", 'sys.platform == "grafx-os"'),
    ("version question wearing the mark", "sys.version_info >= (3, 0)"),
    ("truthiness of the reading", "sys.platform"),
)


@pytest.mark.parametrize(
    ("label", "condition"),
    CONDITIONS_NAMING_NO_FAMILY,
    ids=[row[0] for row in CONDITIONS_NAMING_NO_FAMILY],
)
def test_a_condition_naming_no_family_does_not_attribute(
    label: str, condition: str, tmp_path: Path
) -> None:
    # A89: the author can invent a comparison; they cannot invent an operating system. Without
    # the closed set, sys.platform != "" read as a genuine family condition while its
    # counterpart sys.platform == "" ran on no family at all -- both halves green, the test
    # skipped everywhere, forever.
    module = (
        HEADER
        + "HARDCODED_TRUE = True\n\n\n"
        + f'@pytest.mark.skipif({condition}, reason="claims a family")\n'
        + "@pytest.mark.platform_specific\n"
        + "def test_planted() -> None:\n"
        + '    assert 1 + 1 == 3, "this would FAIL on any family"\n'
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, f"{label} passed as a family condition:\n{output}"


REAL_FAMILY_CONDITIONS: tuple[tuple[str, str], ...] = (
    ("sys.platform equality", 'sys.platform == "win32"'),
    ("os.name equality", 'os.name == "nt"'),
    ("os.name posix", 'os.name == "posix"'),
    ("membership in supported values", 'sys.platform in ("win32", "linux")'),
    ("a resolved flag", "IS_WINDOWS"),
    ("a negated resolved flag", "not IS_WINDOWS"),
)


@pytest.mark.parametrize(
    ("label", "condition"),
    REAL_FAMILY_CONDITIONS,
    ids=[row[0] for row in REAL_FAMILY_CONDITIONS],
)
def test_a_condition_naming_a_real_family_still_attributes(
    label: str, condition: str, tmp_path: Path
) -> None:
    # The spellings the real suite uses, including a flag bound in another module, must keep
    # working: a gate that fails a correct test teaches authors to route around it.
    module = (
        '"""Planted."""\n\nfrom __future__ import annotations\n\nimport os\nimport sys\n\n'
        + "import pytest\n\n"
        + "__all__: list[str] = []\n\n"
        + 'IS_WINDOWS = os.name == "nt"\n\n\n'
        + f'@pytest.mark.skipif({condition}, reason="one family")\n'
        + "@pytest.mark.platform_specific\n"
        + "def test_planted() -> None:\n    assert True\n"
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 0, f"{label} was refused:\n{output}"


# --- A56: the selection sets decide whether the disappearance rule exists -----------------------

def test_the_selection_sets_are_exactly_pytest_selection_options() -> None:
    # A56: these two lists decide whether the disappearance rule runs at all, so they are pinned
    # by content. Widened by one token -- "-v" here, "verbose" there -- an ordinary verbose run
    # reads as a filter and a collect_ignore vanish of a failing module exits 0. Behaviour cannot
    # pin this: the widened rule is silent by construction, which is the whole defect.
    shared = CONFTEST.read_text(encoding="utf-8")
    flags = shared[shared.index("SELECTION_FLAGS: tuple[str, ...] = (") :]
    flags = flags[: flags.index(")")]
    assert sorted(entry.strip().strip('",') for entry in flags.splitlines()[1:] if entry.strip()) == [
        "--co",
        "--collect-only",
        "--deselect",
        "--failed-first",
        "--ff",
        "--ignore",
        "--ignore-glob",
        "--last-failed",
        "--lf",
        "--stepwise",
        "--stepwise-skip",
        "--sw",
        "-k",
        "-m",
    ]

    options = shared[shared.index("SELECTION_OPTIONS: tuple[str, ...] = (") :]
    options = options[: options.index(")")]
    assert sorted(entry.strip().strip('",') for entry in options.splitlines()[1:] if entry.strip()) == [
        "deselect",
        "failedfirst",
        "ff",
        "ignore",
        "ignore_glob",
        "keyword",
        "last_failed",
        "lastfailed",
        "lf",
        "markexpr",
        "stepwise",
        "stepwise_skip",
        "sw",
    ]


def test_a_selection_flag_is_never_a_mere_reporting_option() -> None:
    # The property behind the pin, stated so a future edit reads the intent and not just a list.
    shared = CONFTEST.read_text(encoding="utf-8")
    for reporting_only in ('"-v"', '"-q"', '"--tb"', '"verbose"', '"quiet"', '"capture"'):
        assert f"    {reporting_only}," not in shared, reporting_only


def test_an_ordinary_verbose_run_is_not_a_filter(tmp_path: Path) -> None:
    # The other side of the same rule: -v must not be mistaken for a selection.
    module = HEADER + "def test_planted() -> None:\n    assert True\n"
    code, output = _plant(tmp_path, module=module, extra_arguments=("-v",))
    assert code == 0, output


def test_a_verbose_run_still_catches_a_vanished_module(tmp_path: Path) -> None:
    # The consequence the pin protects: under -v the disappearance rule must still fire.
    nested_conftest = '\n\ncollect_ignore = ["test_nested.py"]\n'
    nested_module = HEADER + "def test_planted() -> None:\n    assert 1 + 1 == 3\n"
    code, output = _plant(
        tmp_path,
        nested_conftest=nested_conftest,
        nested_module=nested_module,
        extra_arguments=("-v",),
    )
    assert code == 1, output


def test_a_hyphenated_distribution_is_checked_by_its_import_name(tmp_path: Path) -> None:
    # A distribution name is not always an import name. pytest-timeout ships pytest_timeout, so
    # asking find_spec about the hyphenated spelling answers "absent" for something installed --
    # the closed set binds the distribution, and absence must be asked of the module it provides.
    module = (
        HEADER
        + '@pytest.mark.optional_dependency("pytest-timeout")\n'
        + "def test_planted() -> None:\n"
        + '    pytest.skip("needs the plugin")\n'
    )
    code, output = _plant(tmp_path, module=module)
    if _find_spec_safe("pytest_timeout") is None:
        assert code == 0, output
        return
    assert code == 1, output


@pytest.mark.parametrize(
    "name", ["we need the windows helper", "numpy helper", "not an identifier!", "12numpy"]
)
def test_a_declared_name_that_is_not_a_module_name_attributes_nothing(
    name: str, tmp_path: Path
) -> None:
    # The name-shape guard, reached now that the manifest itself can carry the spelling: a
    # sentence is not a module, whatever find_spec says when asked about one.
    manifest_extra = f'\nprose = ["{name}"]\n'
    module = (
        HEADER
        + f'@pytest.mark.optional_dependency("{name}")\n'
        + "def test_planted() -> None:\n"
        + '    pytest.skip("needs it")\n'
    )
    code, output = _plant(tmp_path, module=module, manifest_extra=manifest_extra)
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


def test_a_collect_only_run_reconciles_nothing(tmp_path: Path) -> None:
    # _reconcile returns early under --collect-only: no test ran, so "collected but never
    # reported" is true of every one of them and means nothing.
    module = HEADER + "def test_planted() -> None:\n    assert True\n"
    code, output = _plant(tmp_path, module=module, extra_arguments=("--collect-only",))
    assert code == 0, output
    assert "collected but never reported" not in output


def test_a_run_stopped_early_reconciles_nothing(tmp_path: Path) -> None:
    # _reconcile returns early when the run was cut short by maxfail: the tests after the stop
    # never ran by request, and reporting them as vanished would be noise on an already-red run.
    modules = {
        "test_first.py": HEADER + "def test_fails() -> None:\n    assert 1 + 1 == 3\n",
        "test_second.py": HEADER + "def test_never_runs() -> None:\n    assert True\n",
    }
    code, output = _plant(tmp_path, modules=modules, extra_arguments=("-x",))
    assert code != 0, output
    assert "collected but never reported" not in output


def test_a_tryfirst_collection_finish_removal_is_caught(tmp_path: Path) -> None:
    # The critic's shape B: byte-identical to the probe above but for one decorator. A nested
    # conftest registers after the parent, so among equal-priority tryfirst implementations the
    # nested one runs first -- capturing at collection_finish watched "what survived up to my
    # hook", and one decorator walked a module of failing assertions through at exit 0.
    # Capturing at pytest_itemcollected has no earlier hook to be pre-empted by.
    nested_conftest = (
        "\n\n@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_collection_finish(session):\n"
        "    session.items[:] = [i for i in session.items if 'nested' not in i.nodeid]\n"
    )
    nested_module = HEADER + "def test_planted() -> None:\n    assert 1 + 1 == 3\n"
    code, output = _plant(
        tmp_path, nested_conftest=nested_conftest, nested_module=nested_module
    )
    assert code == 1, output
    assert "collected but never reported" in output


def test_a_tryfirst_modifyitems_removal_is_caught(tmp_path: Path) -> None:
    # The same hazard one hook earlier: modifyitems runs before collection_finish entirely, so
    # anything removed there never entered the old capture at all.
    nested_conftest = (
        "\n\n@pytest.hookimpl(tryfirst=True)\n"
        "def pytest_collection_modifyitems(session, config, items):\n"
        "    items[:] = [i for i in items if 'nested' not in i.nodeid]\n"
    )
    nested_module = HEADER + "def test_planted() -> None:\n    assert 1 + 1 == 3\n"
    code, output = _plant(
        tmp_path, nested_conftest=nested_conftest, nested_module=nested_module
    )
    assert code == 1, output
    assert "collected but never reported" in output


def _running_family_condition(*, holds: bool) -> str:
    """Return a real family condition that is true, or false, on THIS interpreter."""
    return f'os.name {"==" if holds else "!="} "{os.name}"'


def test_a_marker_whose_condition_is_false_here_attributes_nothing(tmp_path: Path) -> None:
    # BLOCKER 2. The condition says the test should RUN on this family, so a skip from it is not
    # the family talking -- it is an imperative skip wearing a marker that does not describe it.
    # One added line vanished any correctly paired test, because the marker was the whole
    # payment and nothing ever related it to the skip that happened.
    module = (
        HEADER
        + f'@pytest.mark.skipif({_running_family_condition(holds=False)}, reason="other family")\n'
        + "@pytest.mark.platform_specific\n"
        + "def test_the_important_one() -> None:\n"
        + '    pytest.skip("I simply do not want to run today")\n'
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output
    assert "skipped test(s) carry neither" in output


def test_a_marker_whose_condition_holds_here_still_attributes(tmp_path: Path) -> None:
    # The control: when the condition is true here, the skip IS the family talking.
    module = (
        HEADER
        + f'@pytest.mark.skipif({_running_family_condition(holds=True)}, reason="this family")\n'
        + "@pytest.mark.platform_specific\n"
        + "def test_planted() -> None:\n"
        + "    assert True\n"
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 0, output


def test_an_imperative_skip_cannot_ride_a_counterpart_marker(tmp_path: Path) -> None:
    # The same defect at module scope: a correctly paired module gains one line and the test
    # that would have failed disappears on the family it was supposed to cover.
    module = (
        HEADER
        + f'@pytest.mark.skipif({_running_family_condition(holds=True)}, reason="this family")\n'
        + "@pytest.mark.platform_specific\n"
        + "def test_the_other_family() -> None:\n"
        + "    assert True\n\n\n"
        + f'@pytest.mark.skipif({_running_family_condition(holds=False)}, reason="other family")\n'
        + "@pytest.mark.platform_specific\n"
        + "def test_the_important_one() -> None:\n"
        + '    pytest.skip("not today")\n'
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, output


BOOLOP_FAMILY_CLAIMS: tuple[str, ...] = (
    "IS_WINDOWS or True",
    "True or IS_WINDOWS",
    'sys.platform == "win32" or HARDCODED_TRUE',
    "IS_WINDOWS and True",
)


@pytest.mark.parametrize("condition", BOOLOP_FAMILY_CLAIMS)
def test_a_boolop_never_names_a_family(condition: str, tmp_path: Path) -> None:
    # BLOCKER 1 through the real gate: each of these is true on every family, so the test runs
    # on none, and each was certified by both halves while the constant it contains is refused
    # on its own.
    module = (
        HEADER
        + "HARDCODED_TRUE = True\n"
        + 'IS_WINDOWS = os.name == "nt"\n\n\n'
        + f'@pytest.mark.skipif({condition}, reason="windows only")\n'
        + "@pytest.mark.platform_specific\n"
        + "def test_the_important_one() -> None:\n"
        + '    raise AssertionError("would fail on any family")\n'
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, f"{condition} passed as a family condition:\n{output}"


def test_a_prefix_named_test_is_part_of_the_expectation(tmp_path: Path) -> None:
    # BLOCKER 3 through the real gate: python_functions defaults to the PREFIX "test", so
    # def testalpha is collected and run. Counting only "test_" made a module of such tests
    # invisible to the expectation, and removing it cost nothing at all.
    nested_conftest = '\n\ncollect_ignore = ["test_nested.py"]\n'
    nested_module = (
        HEADER + "def testalpha() -> None:\n    assert 1 + 1 == 3\n"
    )
    code, output = _plant(
        tmp_path, nested_conftest=nested_conftest, nested_module=nested_module
    )
    assert code == 1, output
    assert "vanished" in output


def test_a_prefix_named_test_in_a_prefix_named_class_counts_too(tmp_path: Path) -> None:
    nested_conftest = '\n\ncollect_ignore = ["test_nested.py"]\n'
    nested_module = (
        HEADER
        + "class TestGroup:\n"
        + "    def testbeta(self) -> None:\n        assert 1 + 1 == 3\n"
    )
    code, output = _plant(
        tmp_path, nested_conftest=nested_conftest, nested_module=nested_module
    )
    assert code == 1, output
    assert "vanished" in output


UNSUPPORTED_PLATFORM_VALUES: tuple[str, ...] = (
    # Real values a reading can return, and targets this project does not ship to (D9). A
    # condition naming one runs on no family we test, so it is attributed-skipped forever.
    'sys.platform != "emscripten"',
    'sys.platform != "aix"',
    'sys.platform != "wasi"',
    'sys.platform == "sunos"',
    # Real value, wrong reading: os.name never returns win32.
    'os.name != "win32"',
    'os.name == "linux"',
    'platform.system() == "nt"',
)


@pytest.mark.parametrize("condition", UNSUPPORTED_PLATFORM_VALUES)
def test_a_platform_outside_the_support_matrix_names_no_family(
    condition: str, tmp_path: Path
) -> None:
    # B1: the closed set was the union of everything a reading CAN return, which includes
    # platforms nobody targets. The set has to be the support matrix -- a fact about this
    # project, not about CPython -- and it is kept per reading, so a real value paired with the
    # wrong reading is refused too.
    module = (
        HEADER
        + "import platform\n\n\n"
        + f'@pytest.mark.skipif({condition}, reason="claims a family")\n'
        + "@pytest.mark.platform_specific\n"
        + "def test_the_important_one() -> None:\n"
        + '    raise AssertionError("would fail on any target")\n'
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, f"{condition} passed as a family condition:\n{output}"


REBINDING_SPELLINGS: tuple[tuple[str, str], ...] = (
    ("plain rebind", "FAMILY = {here}\nFAMILY = False"),
    ("rebind to not", "FAMILY = {here}\nFAMILY = not FAMILY"),
    ("augmented", "FAMILY = {here}\nFAMILY &= True"),
    ("annotated rebind", "FAMILY = {here}\nFAMILY: bool = True"),
    ("tuple unpack", "FAMILY = {here}\nFAMILY, OTHER = True, False"),
    ("del and reassign", "FAMILY = {here}\ndel FAMILY\nFAMILY = True"),
    ("rebind inside if", "FAMILY = {here}\nif True:\n    FAMILY = True"),
    ("walrus rebind", "FAMILY = {here}\n_ = (FAMILY := True)"),
)


@pytest.mark.parametrize(
    ("label", "template"), REBINDING_SPELLINGS, ids=[row[0] for row in REBINDING_SPELLINGS]
)
def test_a_name_bound_more_than_once_names_no_family(
    label: str, template: str, tmp_path: Path
) -> None:
    # B2: pytest evaluates the LAST binding; reading the first kept the claim and changed the
    # outcome. Following the rebinding would be a dataflow analysis and it would lose, so two
    # bindings is a refusal. One unambiguous binding is a closed condition; anything else is not.
    bindings = template.format(here=f'os.name == "{os.name}"')
    module = (
        HEADER
        + bindings
        + "\n\n\n"
        + '@pytest.mark.skipif(FAMILY, reason="claims a family")\n'
        + "@pytest.mark.platform_specific\n"
        + "def test_the_important_one() -> None:\n"
        + '    pytest.skip("gone")\n'
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 1, f"{label} kept its claim across a rebinding:\n{output}"


def test_a_single_binding_still_names_a_family(tmp_path: Path) -> None:
    module = (
        HEADER
        + f'FAMILY = os.name == "{os.name}"\n\n\n'
        + '@pytest.mark.skipif(FAMILY, reason="this family")\n'
        + "@pytest.mark.platform_specific\n"
        + "def test_planted() -> None:\n    assert True\n"
    )
    code, output = _plant(tmp_path, module=module)
    assert code == 0, output


COLLECTED_BUT_NOT_A_LITERAL_DEF: tuple[tuple[str, str], ...] = (
    (
        "built by a factory",
        "def _make():\n    def inner() -> None:\n        assert 1 + 1 == 3\n    return inner\n\n\n"
        "test_dynamic = _make()\n",
    ),
    (
        "def under if True",
        "if True:\n    def test_nested() -> None:\n        assert 1 + 1 == 3\n",
    ),
    (
        "def in a for body",
        "for _ in range(1):\n    def test_looped() -> None:\n        assert 1 + 1 == 3\n",
    ),
    (
        "def in a try body",
        "try:\n    def test_guarded() -> None:\n        assert 1 + 1 == 3\n"
        "except Exception:\n    pass\n",
    ),
    (
        "class made with type",
        "def _body(self) -> None:\n    assert 1 + 1 == 3\n\n\n"
        'TestMade = type("TestMade", (), {"test_one": _body})\n',
    ),
    (
        "class nested in a collected class",
        "class TestOuter:\n    class TestInner:\n"
        "        def test_one(self) -> None:\n            assert 1 + 1 == 3\n",
    ),
)


@pytest.mark.parametrize(
    ("label", "body"),
    COLLECTED_BUT_NOT_A_LITERAL_DEF,
    ids=[row[0] for row in COLLECTED_BUT_NOT_A_LITERAL_DEF],
)
def test_a_module_pytest_collects_is_expected_however_its_tests_are_written(
    label: str, body: str, tmp_path: Path
) -> None:
    # B3: the expectation counted literal def nodes, so pytest collected and ran these while the
    # gate saw zero tests -- and each module could then be removed with nothing paid at all. The
    # authority is now pytest's own collection, with a deliberately generous source floor under
    # it: a name matching pytest's patterns anywhere is enough to expect the module to appear.
    nested_conftest = '\n\ncollect_ignore = ["test_nested.py"]\n'
    code, output = _plant(
        tmp_path, nested_conftest=nested_conftest, nested_module=HEADER + body
    )
    assert code == 1, f"{label} vanished for free:\n{output}"
    assert "vanished" in output
