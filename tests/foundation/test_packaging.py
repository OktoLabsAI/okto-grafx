"""Packaging is part of the contract: pure Python, one wheel, zero runtime dependencies (TR-9)."""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from typing import Any

import pytest

import okto_grafx

PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
PYPROJECT: Path = PROJECT_ROOT / "pyproject.toml"


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


def test_there_is_no_runtime_dependency(manifest: dict[str, Any]) -> None:
    # G3: the wheel is pure Python and the stdlib is the only runtime requirement.
    assert manifest["project"]["dependencies"] == []


def test_optional_dependencies_are_the_declared_extras(manifest: dict[str, Any]) -> None:
    extras = manifest["project"]["optional-dependencies"]
    assert extras == {
        "accel": ["numpy>=1.24"],
        "bench": ["ladybug==0.16.0", "numpy>=1.24"],
        "dev": ["pytest>=8", "pytest-timeout"],
    }


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
    declared = {entry.split(":", 1)[0] for entry in options["markers"]}
    assert declared == {"platform_specific", "slow", "multiprocess", "bench"}
    for entry in options["markers"]:
        assert ":" in entry, f"marker {entry!r} has no description"


def test_the_package_imports_from_a_clean_interpreter() -> None:
    assert isinstance(okto_grafx.__version__, str)
    assert okto_grafx.__all__ == ["__version__"]
    assert okto_grafx.__doc__


def test_the_public_package_does_not_pull_in_the_engine_yet() -> None:
    # C11 adds the facade; until then importing the package must not require modules that do
    # not exist, which is what keeps every wave independently importable.
    assert not hasattr(okto_grafx, "connect")
