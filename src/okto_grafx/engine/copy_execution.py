"""Private native copy of flexible/no-PK or independently labeled entities.

This is not a public row-staging door. The copy facade validates the detached
package, target schemas, endpoint closure, bounds and spaces before entering it
under the target participant section. All entity bindings below are created or
read by native operators in this target transaction; none comes from a caller.
"""

from __future__ import annotations

from dataclasses import replace

from okto_grafx.domain.index.keys import index_key
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.query.analysis import QueryAnalysis
from okto_grafx.domain.query.ast import Direction, MapEntry, MapExpression, Parameter, Variable
from okto_grafx.domain.query.plan import (
    CreatedNode, CreatedRelationship, CreateRelationships, SingleRow, IndexSeek, LabelAssignment, SetProperties,
)
from okto_grafx.engine.index_manager import primary_key_index_name
from okto_grafx.engine.public_views import _query_parameters_snapshot
from okto_grafx.engine.query_engine import (
    QueryEngine, _Context, _Row, _write_one, _require_write_transaction, _iterator_cleanup,
)
from okto_grafx.errors import GrafxConfigurationError, GrafxUnsupportedOperation


def _stage_copy_entities(engine, txn, catalog, targets, rows, *, conflict, max_value_characters):
    """Create independently identified rows through native operators and one staging mark."""
    if type(engine) is not QueryEngine:
        raise GrafxUnsupportedOperation("Identity-remapped copy requires the native query engine.",
                                         operation="catalog_copy")
    _require_write_transaction(txn)
    marker = txn.staging_mark()
    try:
        created = _stage(engine, txn, catalog, targets, rows, conflict, max_value_characters)
        txn.settle_staging_mark(marker)
        return created
    except BaseException:
        txn.discard_since(marker)
        raise


def _stage(engine, txn, catalog, targets, rows, conflict, max_value_characters):
    anchor = SingleRow()
    context = _Context(engine=engine, txn=txn, parameters={},
                       analysis=QueryAnalysis(engine.parse("RETURN 1")), statistics={},
                       coalesce_types={}, case_types={}, timestamp_values={}, catalog=catalog,
                       atomic_staging=True,
                       index_authority=engine._statement_index_authority(catalog, txn=txn))
    identities = {}
    keyed = {}
    nodes_published = False
    created = 0
    for source, rid, values, labels in rows:
        context.admit_intermediate(anchor)
        target = context.schema().table_by_id(targets[source.kind, source.name].table_id)
        if source.kind == "node":
            key = None
            if source.primary_key is not None:
                pk = values[source.column_index(source.primary_key)]
                key = (source.name, index_key((pk,), (0,)))
            if conflict == "skip" and key is not None:
                binding = keyed.get(key)
                if binding is None:
                    # Copy selects a physical schema, not a logical label. Reuse
                    # native exact seeks/overlays/OCC, including nodes whose base
                    # label was removed and excluding other owners sharing it.
                    plan = IndexSeek(anchor, "n", target, primary_key_index_name(target.name),
                                     IndexVisibility.EXACT, (target.primary_key,), (Parameter("key"),))
                    context.parameters = {"key": pk}
                    stream = engine._rows(plan, context)
                    with _iterator_cleanup(lambda: (stream,)):
                        matches = tuple(stream)
                    if len(matches) > 1:
                        raise GrafxConfigurationError("Copy target has ambiguous primary-key identity.", field="target_primary_key")
                    binding = matches[0].bindings["n"] if matches else None
                if binding is not None:
                    identities[source.name, rid] = binding
                    keyed[key] = binding
                    continue
            properties = _properties(target, values, context, max_value_characters)
            operation = CreateRelationships(anchor, (CreatedNode("n", target, properties, labels=labels or ()),))
            binding = _write_one(engine, operation, _Row(bindings={}), context).bindings["n"]
            if labels == () and not target.unlabeled:
                # Empty written CREATE labels historically mean implicit owner
                # membership. Remove that owner through the normal native label
                # operator in this same staging scope; no raw write authority.
                operation = SetProperties(anchor, (LabelAssignment(Variable("n"), (target.name,), remove=True),))
                binding = _write_one(engine, operation, _Row(bindings={"n": binding}), context).bindings["n"]
            identities[source.name, rid] = binding
            if key is not None:
                keyed[key] = binding
        else:
            if not nodes_published:
                context.publish_phase()
                identities = {key: context.resolve_binding(binding) for key, binding in identities.items()}
                nodes_published = True
            left = identities[source.from_table, values[0]]
            right = identities[source.to_table, values[1]]
            properties = _properties(target, values, context, max_value_characters)
            operation = CreateRelationships(anchor, (), (
                CreatedRelationship("r", target, "a", "b", Direction.OUTGOING, properties),
            ))
            _write_one(engine, operation,
                       _Row(bindings={"a": replace(left, variable="a"), "b": replace(right, variable="b")}), context)
        created += 1
    context.release()
    return created


def _properties(table, values, context, max_value_characters):
    """Build parameter expressions, never source-generated query syntax or fake IDs."""
    offset = 0 if table.kind == "node" else 2
    pairs = tuple(values[-1].items()) if table.flexible_properties else tuple(
        (column.name, values[i]) for i, column in enumerate(table.columns) if i >= offset)
    context.parameters = _query_parameters_snapshot(
        {f"p{i}": value for i, (_key, value) in enumerate(pairs)},
        max_string_characters=max_value_characters)
    return MapExpression(tuple(MapEntry(key, Parameter(f"p{i}")) for i, (key, _value) in enumerate(pairs)))


__all__ = [
]
