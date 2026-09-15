"""Resolver precedence, finite discovery, scope opt-ins and no filesystem writes."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from okto_grafx import CatalogPathPolicy
from okto_grafx.errors import GrafxConfigurationError
from okto_grafx.workspace import WorkspacePolicy, resolve_workspace


@pytest.fixture
def tree(tmp_path):
    project = tmp_path / "project"
    child = project / "a" / "b"
    child.mkdir(parents=True)
    (project / ".git").mkdir()
    return (
        tmp_path,
        project,
        child,
        WorkspacePolicy(CatalogPathPolicy((str(tmp_path),))),
    )


def test_precedence_and_no_creation(tree):
    root, project, child, policy = tree
    before = set(root.rglob("*"))
    marker = resolve_workspace(policy=policy, cwd=child)
    assert (marker.root, marker.project_store, marker.source) == (
        str(project),
        str(project / ".grafx/store"),
        "marker",
    )
    explicit = resolve_workspace(
        policy=policy, explicit_store=root / "custom", project_root=root, cwd=child
    )
    assert (explicit.root, explicit.project_store, explicit.source) == (
        str(root),
        str(root / "custom"),
        "explicit",
    )
    selected = resolve_workspace(policy=policy, project_root=child, cwd=root)
    assert selected.root == str(child) and selected.source == "root"
    assert set(root.rglob("*")) == before


def test_bound_and_cwd_opt_in(tree):
    _, _, child, policy = tree
    bounded = WorkspacePolicy(policy.paths, max_parent_steps=1)
    with pytest.raises(GrafxConfigurationError):
        resolve_workspace(policy=bounded, cwd=child)
    result = resolve_workspace(
        policy=WorkspacePolicy(policy.paths, max_parent_steps=1, allow_cwd=True),
        cwd=child,
    )
    assert result.root == str(child) and result.source == "cwd"
    with pytest.raises(GrafxConfigurationError):
        resolve_workspace(policy=policy)


def test_discovery_never_escapes_allowlist(tree):
    _, _, child, _ = tree
    with pytest.raises(GrafxConfigurationError):
        resolve_workspace(
            policy=WorkspacePolicy(CatalogPathPolicy((str(child),))), cwd=child
        )


def test_user_store_opt_in_and_no_default_home(tree, monkeypatch):
    root, project, _, policy = tree
    monkeypatch.setenv("GRAFX_ROOT", str(root))
    with pytest.raises(GrafxConfigurationError):
        resolve_workspace(policy=policy)
    with pytest.raises(GrafxConfigurationError):
        resolve_workspace(policy=policy, project_root=project, user_store=root / "user")
    enabled = WorkspacePolicy(policy.paths, allow_user_store=True)
    assert resolve_workspace(policy=enabled, project_root=project).user_store is None
    assert resolve_workspace(
        policy=enabled, project_root=project, user_store=root / "user"
    ).user_store == str(root / "user")
    with pytest.raises(GrafxConfigurationError):
        resolve_workspace(
            policy=enabled, project_root=project, user_store=project / ".grafx/store"
        )


@pytest.mark.parametrize(
    "change",
    [
        {"markers": ("..",)},
        {"markers": ("x/y",)},
        {"markers": ("x\\y",)},
        {"markers": ()},
        {"markers": (".git", ".git")},
        {"max_parent_steps": True},
        {"max_parent_steps": 65},
        {"allow_cwd": 1},
        {"allow_user_store": 1},
        {"project_store_name": "../store"},
        {"project_store_name": "/store"},
        {"project_store_name": "C:/store"},
        {"project_store_name": "a//b"},
    ],
)
def test_invalid_policy(tree, change):
    with pytest.raises(GrafxConfigurationError):
        WorkspacePolicy(tree[3].paths, **change)


@pytest.mark.parametrize(
    "path", ["https://example.com/db", "//host/share/db", "\\\\host\\share", ""]
)
def test_remote_paths_refused(tree, path):
    with pytest.raises(GrafxConfigurationError):
        tree[3].paths.resolve(path, existing=False)


def test_two_processes_resolve_identically(tree):
    root, project, child, _ = tree
    script = (
        "import json,sys; from dataclasses import asdict; "
        "from okto_grafx import CatalogPathPolicy; "
        "from okto_grafx.workspace import WorkspacePolicy,resolve_workspace; "
        "print(json.dumps(asdict(resolve_workspace(policy=WorkspacePolicy(CatalogPathPolicy((sys.argv[1],))),cwd=sys.argv[2]))))"
    )
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    results = [
        subprocess.run(
            [sys.executable, "-c", script, str(root), str(child)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        for _ in range(2)
    ]
    assert results[0].stdout == results[1].stdout
    assert json.loads(results[0].stdout)["root"] == str(project)


def test_symlink_or_junction_refused(tree):
    root, project, _, policy = tree
    link = root / "link"
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(project)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(project, target_is_directory=True)
    try:
        with pytest.raises(GrafxConfigurationError):
            resolve_workspace(policy=policy, project_root=link)
    finally:
        if os.name == "nt":
            link.rmdir()  # remove only the junction, not its target
        else:
            link.unlink()


@pytest.mark.platform_specific
@pytest.mark.skipif(os.name != "nt", reason="Windows path alias contract")
@pytest.mark.parametrize("suffix", [".. /outside", "name.", "name ", "file:stream"])
def test_windows_namespace_aliases_refused(tree, suffix):
    root, _, _, policy = tree
    with pytest.raises(GrafxConfigurationError):
        policy.paths.resolve(str(root) + "/" + suffix, existing=False)


def test_path_flag_and_null_refused(tree):
    root, _, _, policy = tree
    with pytest.raises(GrafxConfigurationError):
        policy.paths.resolve(root, existing=1)
    with pytest.raises(GrafxConfigurationError):
        policy.paths.resolve(str(root) + "/bad\x00name", existing=False)
