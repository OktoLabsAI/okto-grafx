"""Optional bounded workspace discovery; never opens, creates or modifies a store."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from okto_grafx.catalogs import CatalogPathPolicy, _refuse

__all__ = ["WorkspacePolicy", "ResolvedWorkspace", "resolve_workspace"]


@dataclass(frozen=True, slots=True)
class WorkspacePolicy:
    """Opt-in discovery within explicit allowed roots, with no environment/home fallback."""

    paths: CatalogPathPolicy
    markers: tuple[str, ...] = (".git",)
    max_parent_steps: int = 8
    allow_cwd: bool = False
    allow_user_store: bool = False
    project_store_name: str = ".grafx/store"

    def __post_init__(self) -> None:
        if type(self.paths) is not CatalogPathPolicy:
            _refuse("Expected CatalogPathPolicy.", "paths")
        if type(self.markers) is not tuple or not 1 <= len(self.markers) <= 16:
            _refuse("Declare 1..16 marker names.", "markers")
        for marker in self.markers:
            if (
                type(marker) is not str
                or not marker
                or marker in (".", "..")
                or any(c in marker for c in "/\\:\x00")
            ):
                _refuse("Markers must be single local names.", "markers")
        if len(set(self.markers)) != len(self.markers):
            _refuse("Duplicate marker.", "markers")
        if (
            type(self.max_parent_steps) is not int
            or not 0 <= self.max_parent_steps <= 64
        ):
            _refuse("max_parent_steps must be 0..64.", "max_parent_steps")
        if type(self.allow_cwd) is not bool or type(self.allow_user_store) is not bool:
            _refuse("Scope opt-ins must be boolean.", "policy")
        name = self.project_store_name
        if (
            type(name) is not str
            or not name
            or "\\" in name
            or ":" in name
            or "\x00" in name
            or any(p in ("", ".", "..") for p in name.split("/"))
        ):
            _refuse(
                "project_store_name must be a normalized relative path.",
                "project_store_name",
            )


@dataclass(frozen=True, slots=True)
class ResolvedWorkspace:
    """Explicit absolute paths, not open handles; source records which precedence rule won."""

    root: str
    project_store: str
    user_store: str | None
    source: str


def resolve_workspace(
    *,
    policy: WorkspacePolicy,
    explicit_store: str | Path | None = None,
    project_root: str | Path | None = None,
    cwd: str | Path | None = None,
    user_store: str | Path | None = None,
) -> ResolvedWorkspace:
    """Resolve explicit store > project root > bounded marker > opted-in cwd. No mkdir/open."""
    if type(policy) is not WorkspacePolicy:
        _refuse("Expected WorkspacePolicy.", "policy")
    paths = policy.paths
    selected_user = None
    if user_store is not None:
        if not policy.allow_user_store:
            _refuse("User/global scope requires explicit opt-in.", "user_store")
        selected_user = paths.resolve(user_store, existing=False)
    if explicit_store is not None:
        store = paths.resolve(explicit_store, existing=False)
        root = paths.resolve(
            project_root if project_root is not None else Path(store).parent
        )
        source = "explicit"
    elif project_root is not None:
        root = paths.resolve(project_root)
        store = paths.resolve(Path(root) / policy.project_store_name, existing=False)
        source = "root"
    else:
        if cwd is None:
            _refuse("Supply a project root, explicit store or discovery cwd.", "cwd")
        start = Path(paths.resolve(cwd))
        candidate = start
        root = None
        for _ in range(policy.max_parent_steps + 1):
            for marker in policy.markers:
                marker_path = Path(paths.resolve(candidate / marker, existing=False))
                if marker_path.exists():
                    root = str(candidate)
                    break
            if root is not None:
                break
            parent = candidate.parent
            if parent == candidate or not any(
                parent.is_relative_to(Path(r)) for r in paths.allowed_roots
            ):
                break
            candidate = Path(paths.resolve(parent))
        if root is None:
            if not policy.allow_cwd:
                _refuse("No workspace marker within the discovery bound.", "workspace")
            root, source = str(start), "cwd"
        else:
            source = "marker"
        store = paths.resolve(Path(root) / policy.project_store_name, existing=False)
    if selected_user is not None and os.path.normcase(
        selected_user
    ) == os.path.normcase(store):
        _refuse("Project and user store must be distinct.", "user_store")
    return ResolvedWorkspace(root, store, selected_user, source)
