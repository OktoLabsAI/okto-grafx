"""Read-only consumption of public Grafx metadata and search DTOs."""

from dataclasses import fields, is_dataclass
from enum import Enum
import json
import math

from okto_grafx import __version__
from okto_grafx.domain.errors import GrafxConfigurationError
from okto_grafx.domain.vector.filter import RecordIdFilter


def dto(value):
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


def capabilities(invocation):
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
        },
        "transactions": {
            "scope": "single_store",
            "isolation": "snapshot_occ",
            "concurrent_readers": True,
            "concurrent_writers": True,
        },
    }
    return Report(0, payload, (f"Okto Grafx {__version__}: build CLI contracts",))


def inspect_indexes(invocation, database):
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


def search(invocation, database):
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
