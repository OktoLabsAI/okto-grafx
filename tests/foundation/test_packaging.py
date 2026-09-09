"""Packaging is part of the contract: pure Python, one wheel, zero runtime dependencies (TR-9)."""

from __future__ import annotations

import ast
import importlib.util
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest

import okto_grafx

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
PYPROJECT: Path = PROJECT_ROOT / "pyproject.toml"
CI_WORKFLOW: Path = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def manifest() -> dict[str, Any]:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_distribution_identity(manifest: dict[str, Any]) -> None:
    project = manifest["project"]
    assert project["name"] == "okto-grafx"
    assert project["requires-python"] == ">=3.11"
    assert project["version"] == okto_grafx.__version__


def test_the_build_uses_setuptools_with_a_src_layout(manifest: dict[str, Any]) -> None:
    build_system = manifest["build-system"]
    assert build_system["build-backend"] == "setuptools.build_meta"
    assert any(requirement.startswith("setuptools") for requirement in build_system["requires"])
    assert manifest["tool"]["setuptools"]["packages"]["find"]["where"] == ["src"]


def test_package_discovery_excludes_optional_sibling_wheels(
    manifest: dict[str, Any], tmp_path: Path,
) -> None:
    from setuptools import find_namespace_packages

    for name in ("okto_grafx/api", "okto_grafx_agent", "okto_grafx_mcp",
                 "okto_grafx_workspace", "okto_grafx_algorithms", "okto_grafx_interop"):
        (tmp_path / name).mkdir(parents=True)
    # Prove the historical selector actually admits the unwanted package; a
    # non-discoverable sentinel would make the new exclusion test vacuous.
    legacy = find_namespace_packages(str(tmp_path), include=["okto_grafx*"])
    assert "okto_grafx_agent" in legacy and "okto_grafx_mcp" in legacy
    selection = manifest["tool"]["setuptools"]["packages"]["find"]
    actual = find_namespace_packages(str(tmp_path), include=selection["include"])
    assert set(actual) == {"okto_grafx", "okto_grafx.api"}


def test_there_is_no_runtime_dependency(manifest: dict[str, Any]) -> None:
    # G3: the wheel is pure Python and the stdlib is the only runtime requirement.
    assert manifest["project"]["dependencies"] == []


def test_optional_dependencies_are_the_declared_extras(manifest: dict[str, Any]) -> None:
    # G3 holds because none of this is a RUNTIME dependency: the core computes every checksum and
    # every distance in pure Python, and an extra only replaces an implementation with a faster
    # one that had to reproduce the reference before it was allowed to run (D2).
    extras = manifest["project"]["optional-dependencies"]
    assert extras == {
        "accel": ["numpy>=1.24", "google-crc32c>=1.5"],
        "bench": ["ladybug==0.16.0", "numpy>=1.24"],
        "dev": ["pytest>=8", "pytest-timeout", "ruff==0.15.1", "PyYAML>=6"],
    }


def test_the_linter_baseline_is_pinned_and_enforced_by_ci(
    manifest: dict[str, Any],
) -> None:
    """The zero-diagnostic baseline is reproducible and cannot silently leave the workflow."""
    ruff = manifest["tool"]["ruff"]
    assert ruff["target-version"] == "py311"
    assert ruff["lint"]["select"] == ["E4", "E7", "E9", "F"]

    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index("  lint:\n")
    end = workflow.index("\n  suite:\n", start)
    lint_job = workflow[start:end]
    assert 'python -m pip install -e ".[dev]"' in lint_job
    assert "python -m ruff check ." in lint_job


def test_every_accelerator_the_accel_extra_names_is_one_an_adapter_knows_how_to_use(
    manifest: dict[str, Any],
) -> None:
    """An extra that installs something nothing can bind is a dependency bought for nothing."""
    from okto_grafx.adapters.checksum_native import CRC32C_PROVIDERS

    accel = manifest["project"]["optional-dependencies"]["accel"]
    distributions = {requirement.split(">=")[0].split("==")[0] for requirement in accel}
    known = {module.replace("_", "-") for module, _attribute in CRC32C_PROVIDERS}
    assert distributions & known, (
        f"the accel extra names {sorted(distributions)}, and the checksum adapter accepts "
        f"{sorted(known)}"
    )


def test_no_console_script_points_at_a_module_that_cannot_be_imported(
    manifest: dict[str, Any]
) -> None:
    # A wheel that installs a command whose module does not exist ships a broken command. The
    # oktografx entry point arrives with C12, together with okto_grafx/cli/main.py.
    scripts = manifest["project"].get("scripts", {})
    for command, target in scripts.items():
        module, _, function = target.partition(":")
        assert function, f"{command} does not name a callable"
        assert importlib.util.find_spec(module) is not None, (
            f"console script {command!r} points at {module!r}, which cannot be imported"
        )


def test_the_cli_entry_point_is_not_declared_before_its_module_exists(
    manifest: dict[str, Any]
) -> None:
    assert importlib.util.find_spec("okto_grafx") is not None
    if "oktografx" in manifest["project"].get("scripts", {}):
        assert importlib.util.find_spec("okto_grafx.cli") is not None


def test_the_package_ships_its_typing_marker(manifest: dict[str, Any]) -> None:
    assert manifest["tool"]["setuptools"]["package-data"]["okto_grafx"] == ["py.typed"]
    assert (PROJECT_ROOT / "src" / "okto_grafx" / "py.typed").is_file()


def test_pytest_configuration_declares_every_marker(manifest: dict[str, Any]) -> None:
    options = manifest["tool"]["pytest"]["ini_options"]
    assert options["testpaths"] == ["tests"]
    assert options["pythonpath"] == ["src"]
    assert "--strict-markers" in options["addopts"]
    # optional_dependency takes an argument, so the registration reads
    # "optional_dependency(module): ..." and the name is what precedes the parenthesis.
    declared = {entry.split(":", 1)[0].split("(", 1)[0] for entry in options["markers"]}
    # "pending" is the one non-platform attribution a skip may carry (amendment A35), and it
    # has to be registered because --strict-markers is on.
    assert declared == {
        "platform_specific",
        "slow",
        "multiprocess",
        "bench",
        "pending",
        "optional_dependency",
    }
    for entry in options["markers"]:
        assert ":" in entry, f"marker {entry!r} has no description"


def test_the_package_imports_from_a_clean_interpreter() -> None:
    assert isinstance(okto_grafx.__version__, str)
    assert okto_grafx.__doc__
    # C11 landed the facade, so the surface is no longer just the version. Pinned as a list so a
    # name joins the public API by decision rather than by drifting in, and every entry is
    # resolved: an __all__ that names something the module does not define is a broken promise
    # to `from okto_grafx import *`, and it is exported code that no import ever exercises.
    assert okto_grafx.__all__ == [
        "CancellationToken",
        "CommitCatalogEntry",
        "CommitHistoryPage",
        "CommitId",
        "CommitImport",
        "CommitKind",
        "CommitMapping",
        "CommitMetadata",
        "ConnectOptions",
        "Database",
        "DatabaseConfig",
        "DatabaseIdentity",
        "ExecuteManyReport",
        "HybridHit",
        "HybridSearchOptions",
        "HybridSearchResult",
        "MetadataLimits",
        "PortRegistry",
        "Query",
        "QueryCursor",
        "QueryResult",
        "ScanCursorV1",
        "ScanPageV1",
        "ScanRowV1",
        "TextHit",
        "TextIndexOptions",
        "TextSearchLimits",
        "TextSearchResult",
        "Timestamp",
        "Transaction",
        "VectorValue",
        "__version__",
        "connect",
        "prepare_commit_import",
    ]
    assert okto_grafx.__all__ == sorted(okto_grafx.__all__), "__all__ is not sorted"
    assert len(set(okto_grafx.__all__)) == len(okto_grafx.__all__), "__all__ repeats a name"
    for name in okto_grafx.__all__:
        assert hasattr(okto_grafx, name), f"__all__ exports {name!r}, which does not resolve"
    assert okto_grafx.Timestamp.__module__ == "okto_grafx.domain.model.value"
    assert okto_grafx.VectorValue.__module__ == "okto_grafx.domain.model.value"
    assert okto_grafx.ScanCursorV1.__module__ == "okto_grafx.engine.database"
    assert okto_grafx.ExecuteManyReport.__module__ == "okto_grafx.engine.database"
    assert okto_grafx.Query.__module__ == "okto_grafx.engine.database"
    assert okto_grafx.QueryCursor.__module__ == "okto_grafx.engine.database"
    assert okto_grafx.ScanPageV1.__module__ == "okto_grafx.engine.database"
    assert okto_grafx.ScanRowV1.__module__ == "okto_grafx.engine.database"


def test_the_public_package_exposes_the_facade() -> None:
    # This was a forward guard asserting `connect` did NOT exist yet, because until C11 landed,
    # importing the package could not be allowed to require modules nobody had written. The
    # forward arrived. The claim worth making now is where `connect` comes from: the package
    # root re-exports the api module rather than growing a second implementation beside it.
    assert callable(okto_grafx.connect)
    assert okto_grafx.connect.__module__ == "okto_grafx.api"


# A61.1: an explicit bound appended for one test, not a relaxation of the suite-wide one.
#
# The suite-wide --timeout=60 exists to turn a hang into a red build (A42). This fixture is not
# a hang risk; it is a `pip wheel` subprocess whose duration this suite does not govern. Measured
# on this machine: 7.6s to copy the tree and 291.1s to build, for 298.6s total, against a bound
# that was 300s -- 99.5% consumed, decided by how many sibling processes happened to be running.
# When it loses that coin flip the thread method calls os._exit and NO junit is written, so the
# whole run reads UNMEASURED (A75.2) rather than failing: one slow fixture erases every other
# result in the file.
#
# So the bound is sized to be unreachable by a legitimately slow build and still finite, because
# firing is always the bad outcome here -- it destroys the report either way. 1800s is ~170x the
# at-rest cost and ~6x the worst contended build measured. A true wedge (a pip lock, a stalled
# child) still turns red; contention no longer can. A78's lesson from the other side: a bound is
# only meaningful against a duration you control, and this one is not, so it buys measurability
# instead of pretending to be a performance budget.
WHEEL_BUILD_TIMEOUT_SECONDS: int = 1800


@pytest.fixture(scope="module")
def built_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the wheel from a pristine copy of the project and return the artefact.

    Hermetic on purpose: building in the repository picks up whatever ``build/`` directory an
    earlier invocation left behind, and a stale directory made this fail for a reason that had
    nothing to do with packaging. A copy has no history, so a failure here is the real thing.
    """
    workspace = tmp_path_factory.mktemp("wheel")
    project = workspace / "project"
    # The WHOLE tree, not a pruned copy. Copying only pyproject and src made tools/, bench/,
    # dashboards/ and tests/ structurally unobservable in the artefact, so the assertion that
    # they do not ship was true by construction rather than by measurement (A60).
    shutil.copytree(
        PROJECT_ROOT,
        project,
        ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "*.egg-info", ".git", "build", "dist", ".pytest_cache",
            ".venv", "venv", "*.db", ".mutation-battery*", ".grafx-tmp",
        ),
    )
    # GX-CAP-0: make optional-package exclusion observable in the real artifact.
    # The former include=["okto_grafx*"] silently bundled these sibling wheels.
    for sibling in ("okto_grafx_agent", "okto_grafx_workspace", "okto_grafx_mcp",
                    "okto_grafx_algorithms", "okto_grafx_interop"):
        package = project / "src" / sibling
        package.mkdir(exist_ok=True)
        (package / "__init__.py").write_text("# packaging boundary sentinel\n", encoding="utf-8")
    output = workspace / "dist"
    build = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            str(project),
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(output),
        ],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, (
        "the wheel could not be built, which is a TR-9 failure:\n"
        + build.stdout[-2000:]
        + build.stderr[-2000:]
    )
    wheels = sorted(output.glob("*.whl"))
    assert wheels, f"pip reported success but produced no wheel:\n{build.stdout[-800:]}"
    return wheels[0]


@pytest.mark.slow
@pytest.mark.timeout(WHEEL_BUILD_TIMEOUT_SECONDS)
def test_the_wheel_was_built_from_a_tree_that_contained_the_other_directories(
    built_wheel: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # The allowlist above is only evidence if the source tree HAD something to exclude. Asserted
    # here so a future pruning of the fixture cannot quietly make the claim vacuous again.
    for directory in ("tools", "tests", "docs"):
        assert (PROJECT_ROOT / directory).is_dir(), directory


@pytest.mark.slow
@pytest.mark.timeout(WHEEL_BUILD_TIMEOUT_SECONDS)
def test_a_built_wheel_actually_contains_the_typing_marker(built_wheel: Path) -> None:
    # The previous assertion read a pyproject substring and checked the file exists on disk;
    # both stay true if the shipping path breaks. This reads the artefact that gets installed.
    with zipfile.ZipFile(built_wheel) as archive:
        names = set(archive.namelist())
        record = next(name for name in names if name.endswith("RECORD"))
        manifest = archive.read(record).decode("utf-8")

    assert "okto_grafx/py.typed" in names, sorted(name for name in names if "typed" in name)
    assert "okto_grafx/py.typed" in manifest
    assert "okto_grafx/__init__.py" in names
    assert "okto_grafx/domain/ports/storage.py" in names
    assert "okto_grafx/runtime/registry.py" in names


@pytest.mark.slow
@pytest.mark.timeout(WHEEL_BUILD_TIMEOUT_SECONDS)
def test_a_built_wheel_ships_the_package_and_nothing_else(built_wheel: Path) -> None:
    with zipfile.ZipFile(built_wheel) as archive:
        names = archive.namelist()
    # An allowlist, not a list of forbidden prefixes: enumerating what must not ship only ever
    # catches the directories somebody thought to name, and the tree grows.
    top_level = {name.split("/")[0] for name in names}
    distribution_info = {name for name in top_level if name.endswith(".dist-info")}
    assert len(distribution_info) == 1, sorted(top_level)
    assert top_level == {"okto_grafx"} | distribution_info, sorted(top_level)
    # TR-9: one pure-Python wheel, so nothing compiled and nothing platform specific.
    assert built_wheel.name.endswith("-py3-none-any.whl"), built_wheel.name
    assert not any(name.endswith((".pyd", ".so", ".dll")) for name in names)


@pytest.mark.slow
@pytest.mark.timeout(WHEEL_BUILD_TIMEOUT_SECONDS)
def test_the_built_wheel_declares_no_runtime_dependency(built_wheel: Path) -> None:
    with zipfile.ZipFile(built_wheel) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_name).decode("utf-8")
    required = [
        line
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist:") and "extra ==" not in line
    ]
    assert required == [], required
    assert "Requires-Python: >=3.11" in metadata


def test_a_per_test_timeout_is_configured(manifest: dict[str, Any]) -> None:
    # A42: a regression that loops forever must be a red build, not an indefinite stall that
    # blocks the machine. Asserted here so the flag cannot be dropped in passing.
    addopts = manifest["tool"]["pytest"]["ini_options"]["addopts"]
    assert "--timeout=" in addopts, addopts
    value = int(addopts.split("--timeout=")[1].split()[0])
    assert 10 <= value <= 300, f"a bound of {value}s is not a useful hang detector"
    # The signal method needs SIGALRM, which Windows does not have, and G4 makes both families
    # equal citizens.
    assert "--timeout-method=thread" in addopts, addopts


def test_the_timeout_plugin_is_declared_and_installed(manifest: dict[str, Any]) -> None:
    development = manifest["project"]["optional-dependencies"]["dev"]
    assert any(requirement.startswith("pytest-timeout") for requirement in development)
    # The flag lives in addopts, so a missing plugin is a loud startup error rather than a
    # timeout that quietly never applies. Assert the plugin is actually here to back it.
    assert importlib.util.find_spec("pytest_timeout") is not None


def test_the_timeout_is_active_in_this_session(request: pytest.FixtureRequest) -> None:
    # A61.1: active and no weaker than the project bound, not equal to it. Asserting equality
    # meant that appending --timeout=120 -- the practice A61 endorses -- turned a green suite
    # red, so two amendments contradicted each other through one assertion.
    active = request.config.getoption("--timeout")
    assert active, "no per-test timeout is active in this session"
    assert float(active) >= 60.0, active


def _explicit_timeout_of(node: ast.FunctionDef) -> int | None:
    """Return the seconds pinned by a ``@pytest.mark.timeout(...)`` decorator, if there is one."""
    for decorator in node.decorator_list:
        if not isinstance(decorator, ast.Call):
            continue
        target = decorator.func
        if not (isinstance(target, ast.Attribute) and target.attr == "timeout"):
            continue
        if not decorator.args:
            return None
        argument = decorator.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, int):
            return argument.value
        if isinstance(argument, ast.Name) and argument.id == "WHEEL_BUILD_TIMEOUT_SECONDS":
            return WHEEL_BUILD_TIMEOUT_SECONDS
    return None


def test_every_test_that_builds_the_wheel_carries_its_own_timeout() -> None:
    """A test that requests ``built_wheel`` and forgets the bound makes the whole file unmeasured.

    The cost of the fixture is charged to whichever test triggers it first, so the hazard is
    decided by collection order rather than by anything the author can see locally. Checked
    structurally so adding a fifth wheel test cannot quietly reintroduce it.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    dependents = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_")
        and any(argument.arg == "built_wheel" for argument in node.args.args)
    ]
    # Anti-vacuity: if the detector finds nothing it must fail, not pass by looking at an empty
    # list. Four wheel tests exist today.
    assert len(dependents) >= 4, [node.name for node in dependents]
    for node in dependents:
        bound = _explicit_timeout_of(node)
        assert bound is not None, (
            f"{node.name} requests built_wheel with no explicit timeout; under load the "
            f"suite-wide bound fires during setup and no junit is written at all"
        )
        assert bound >= WHEEL_BUILD_TIMEOUT_SECONDS, (node.name, bound)
