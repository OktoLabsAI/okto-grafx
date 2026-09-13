"""Read-only consumption of public Grafx metadata and search DTOs."""

from __future__ import annotations

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from okto_grafx.cli.commands import Report
    from okto_grafx.cli.parser import Invocation
    from okto_grafx.engine.database import Database

__all__ = ["dto", "capabilities", "catalog_inventory", "workspace_resolution", "inspect_indexes", "search"]

from dataclasses import fields, is_dataclass
from enum import Enum
import json
import math

from okto_grafx import __version__
from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.vector.filter import RecordIdFilter


def dto(value: object) -> object:
    """Encode engine-owned immutable DTOs explicitly, never their repr or private caches."""
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            field.name: dto(getattr(value, field.name))
            for field in fields(value)
            if not field.name.startswith("_")
        }
    if isinstance(value, (tuple, list)):
        return [dto(item) for item in value]
    return value


def capabilities(invocation: Invocation) -> Report:
    """Report build-level CLI contracts without implying store activation."""
    from okto_grafx.cli.commands import Report
    from okto_grafx.cli.parser import COMMANDS

    payload = {
        "command": "capabilities",
        "schema_version": 1,
        "version": __version__,
        "scope": "build_cli_contracts_not_store_activation",
        "commands": sorted(spec.label for spec in COMMANDS),
        "search": {
            "text": "bm25",
            "vector": "space_metric",
            "hybrid": "rrf",
            "max_k": 1000,
            "partial_results": False,
            "vector_owner_selection": {"table": "optional_if_unique", "table_kind": ["node", "rel"]},
        },
        "transactions": {
            "scope": "single_store",
            "isolation": "snapshot_occ",
            "concurrent_readers": True,
            "concurrent_writers": True,
        },
    }
    return Report(0, payload, (f"Okto Grafx {__version__}: build CLI contracts",))


def catalog_inventory(invocation: Invocation) -> Report:
    """Inspect only explicit read-only attachments; session lifetime ends with this command."""
    from okto_grafx.catalogs import CatalogPathPolicy, CatalogSession, _alias
    from okto_grafx.cli.commands import Report
    policy = CatalogPathPolicy(invocation.repeated("allow_root"))
    attachments = invocation.repeated("attach")
    if len(attachments) > 15:
        raise GrafxConfigurationError("At most 15 attachments are accepted.", field="attach")
    parsed = []
    for item in attachments:
        alias, separator, path = item.partition("=")
        if not separator or not path:
            raise GrafxConfigurationError("Expected ALIAS=PATH.", field="attach")
        parsed.append((_alias(alias), policy.resolve(path)))
    if len({alias for alias, _ in parsed}) != len(parsed):
        raise GrafxConfigurationError("Duplicate attachment alias.", field="attach")
    with CatalogSession.open(invocation.path, policy=policy, read_only=True) as session:
        for alias, path in parsed:
            session.attach(path, alias=alias, read_only=True)
        inventory = session.catalogs()
        limit = invocation.number("limit", 16)
        entries = [dict(alias=item.alias, database_uuid=item.database_uuid.hex(), read_only=item.read_only,
                        owned=item.owned, active_transactions=item.active_transactions) for item in inventory[:limit]]
    return Report(0, dict(command="catalogs", schema_version=1, scope="explicit_invocation_attachments",
                         read_only=True, catalogs=entries, total=len(inventory), truncated=len(inventory) > limit),
                  (f"catalogs {len(entries)}/{len(inventory)} (read-only)",))


def workspace_resolution(invocation: Invocation) -> Report:
    """Expose explicit workspace precedence with no environment or home fallback."""
    from okto_grafx.catalogs import CatalogPathPolicy
    from okto_grafx.workspace import WorkspacePolicy, resolve_workspace
    from okto_grafx.cli.commands import Report
    paths = CatalogPathPolicy(invocation.repeated("allow_root"))
    policy = WorkspacePolicy(paths, markers=invocation.repeated("marker") or (".git",),
        max_parent_steps=invocation.number("max_parent_steps", 8), allow_cwd=invocation.flag("allow_cwd"),
        allow_user_store=invocation.flag("allow_user_store"))
    resolved = resolve_workspace(policy=policy, cwd=invocation.path,
        explicit_store=invocation.text("store") or None, project_root=invocation.text("project_root") or None,
        user_store=invocation.text("user_store") or None)
    return Report(0, dict(command="workspace resolve", schema_version=1, scope="path_resolution_only",
                         stores_opened=0, workspace=dto(resolved)), (f"workspace resolved ({resolved.source})",))


def inspect_indexes(invocation: Invocation, database: Database) -> Report:
    """Return bounded read-only index metadata and explicit truncation counts."""
    from okto_grafx.cli.commands import Report

    limit = invocation.number("limit", 100)
    secondary = database.indexes
    vectors = database.vectors.indexes()
    entries = secondary.indexes()
    payload = {
        "command": "indexes",
        "path": invocation.path,
        "schema_version": 1,
        "read_only": True,
        "observation": "independent_metadata_snapshots",
        "secondary_published_lsn": secondary.published_lsn,
        "indexes": dto(entries[:limit]),
        "vector_indexes": dto(vectors[:limit]),
        "total_indexes": len(entries),
        "total_vector_indexes": len(vectors),
        "truncated": len(entries) > limit or len(vectors) > limit,
    }
    return Report(
        0,
        payload,
        (
            f"indexes {min(limit, len(entries))}/{len(entries)}; "
            f"vector indexes {min(limit, len(vectors))}/{len(vectors)}; "
            f"truncated={payload['truncated']}",
        ),
    )


def _required(invocation, name):
    value = invocation.text(name)
    if not value:
        raise GrafxConfigurationError(f"--{name} is required.", field=name)
    return value


def _array(text, name):
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise GrafxConfigurationError("Expected a JSON array.", field=name) from exc
    if type(value) is not list:
        raise GrafxConfigurationError("Expected a JSON array.", field=name)
    return value


def search(invocation: Invocation, database: Database) -> Report:
    """Validate CLI options and execute one snapshot-bound public search."""
    from okto_grafx.cli.commands import Report

    kind = invocation.spec.subcommand
    k = invocation.number("k", 20)
    if k > 1000:
        raise GrafxConfigurationError("k must be <=1000.", field="k")
    try:
        timeout = float(invocation.text("timeout_seconds", "30"))
    except ValueError as exc:
        raise GrafxConfigurationError(
            "Invalid timeout.", field="timeout_seconds"
        ) from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise GrafxConfigurationError(
            "Timeout must be positive and finite.", field="timeout_seconds"
        )
    selected = None
    if "filter" in invocation.options:
        identifiers = _array(invocation.text("filter"), "filter")
        if any(type(item) is not int for item in identifiers):
            raise GrafxConfigurationError(
                "Filter must contain integer IDs.", field="filter"
            )
        selected = RecordIdFilter(frozenset(identifiers))
    options = {"k": k, "timeout_seconds": timeout}
    if kind != "vector":
        options.update(
            index=_required(invocation, "index"), query=_required(invocation, "query")
        )
    if kind != "text":
        vector = _array(_required(invocation, "vector"), "vector")
        try:
            valid = bool(vector) and all(
                type(v) in (int, float) and math.isfinite(v) for v in vector
            )
        except OverflowError:
            valid = False
        if not valid:
            raise GrafxConfigurationError(
                "Expected finite numeric vector.", field="vector"
            )
        options["space"] = _required(invocation, "space")
        options["query" if kind == "vector" else "vector"] = vector
    if kind == "hybrid":
        options["table"] = _required(invocation, "table")
    elif kind == "vector":
        table_kind = invocation.text("table_kind")
        if "table" in invocation.options or table_kind:
            table = _required(invocation, "table")
            options["table"] = (table_kind, table) if table_kind else table
    options["candidate_filter" if kind == "vector" else "filter"] = selected
    with database.begin("read") as reader:
        operation = {
            "text": database.search_text,
            "vector": database.search_vectors,
            "hybrid": database.search_hybrid,
        }[kind]
        result = operation(reader, **options)
    payload = {
        "command": invocation.spec.label,
        "path": invocation.path,
        "schema_version": 1,
        "search": dto(result),
    }
    return Report(0, payload, (json.dumps(payload["search"], sort_keys=True),))
