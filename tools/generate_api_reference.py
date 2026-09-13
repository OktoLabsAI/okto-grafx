"""Generate the public signature/DTO appendix without importing or opening Grafx.

Only the delimited appendix is generated. Consumer semantics above it stay authored.
Run with --check in CI or without arguments after a public API change.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs/API_REFERENCE.md"
MARKER = "<!-- GENERATED PUBLIC REFERENCE: do not edit below -->"
FACADES = {"Database", "Transaction", "Maintenance", "Query", "QueryCursor"}
DTO_SOURCES = {
    "domain/model/stored_types.py": {"StoredType"},
    "domain/model/decimal_values.py": {"DecimalValue"},
    "domain/model/relationship_type.py": {"RelationshipTypeDef"},
    "domain/temporal.py": {"TemporalCompactionReport", "TemporalGraph", "TemporalLimits", "TemporalPin", "TemporalPruneReport", "TemporalVersion", "TemporalVersions"},
    "views.py": {"ViewParameter", "ViewDefinition"},
    "catalog_copy.py": {"CopyLimits", "CopyTable", "CopyPackage", "CopyReceipt"},
    "catalogs.py": {"CatalogPathPolicy", "CatalogInfo"},
    "workspace.py": {"WorkspacePolicy", "ResolvedWorkspace"},
    "projections.py": {"ProjectionLimits", "ProjectionNode", "ProjectionEdge", "GraphProjection", "ProjectionDiagnostics"},
    "projection_algorithms.py": {"ProjectionLookup", "ProjectionAdjacency", "ProjectionPath", "WeightedProjectionPath", "PageRankResult", "PageRankPreparation", "SimpleTopology", "LabelPropagationResult", "TopologicalOrderResult"},
    "html_snapshot.py": {"HtmlSnapshotLimits"},
    "sqlite_import.py": {"SQLiteImportLimits"},
    "polars.py": {"PolarsFrame"},
    "text_import.py": {"TextImportLimits"},
    "parquet.py": {"ParquetExportReport"},
    "arrow.py": {"ArrowVectorType", "ArrowDecimalType"},
    "domain/query/extensions.py": {"ScalarFunction", "TabularProcedure", "ExtensionRegistry"},
    "domain/query/procedure_writer.py": {"ProcedureResult"},
    "engine/vector_memory.py": {"VectorMemoryUsage", "VectorTotalMemoryUsage"},
    "engine/key_page_memo.py": {"KeyPageCacheUsage"},
    "migrations.py": {"SchemaMigration", "MigrationReport"},
    "engine/index_distribution.py": {"IndexDistribution"},
    "domain/query/hybrid.py": {"HybridSearchOptions", "HybridHit", "HybridSearchResult"},
    "backup.py": {"BackupReport"},
    "transfer.py": {"TransferLimits", "TransferReport", "RecordIdMapping"},
    "domain/index/fulltext.py": {"TextIndexOptions", "TextSearchLimits", "TextHit", "TextSearchResult", "TextMatchPositions"},
    "temporal_diff.py": {"TemporalDiff", "TemporalPropertyChange", "TemporalRowChange", "TemporalSchemaChange"},
    "api/options.py": {"ConnectOptions"},
    "engine/database.py": {
        "DatabaseIdentity",
        "ExecuteManyReport",
        "ScanRowV1",
        "ScanPageV1",
    },
    "engine/query_engine.py": {"QueryResult"},
    "engine/vector_engine.py": {"VectorHit", "VectorSearchResult"},
    "engine/public_views.py": None,
    "engine/index_cleanup.py": None,
    "domain/txn/context.py": {"Snapshot", "CommitReport"},
    "domain/txn/snapshot.py": {"Snapshot"},
    "domain/txn/commit_identity.py": {"CommitId", "CommitTime"},
    "domain/txn/commit_metadata.py": {"CommitMetadata", "MetadataLimits"},
    "domain/txn/commit_catalog.py": {"CommitCatalogEntry", "CommitKind"},
    "domain/txn/commit_history.py": {"CommitHistoryPage"},
    "domain/txn/commit_transfer.py": {"CommitImport", "CommitMapping"},
    "domain/ids.py": {"RecordRef"},
    "domain/wal/replay.py": {"RecycleReport"},
    "domain/recovery/report.py": {"RecoveryReport", "RecoveryFinding"},
    "domain/verify/findings.py": {
        "VerificationReport",
        "VerificationFinding",
        "FindingLocation",
    },
    "domain/model/schema.py": {"TableDef", "ColumnDef", "EmbeddingSpaceDef"},
    "domain/model/value.py": {"Timestamp", "Uuid", "VectorValue"},
    "domain/vector/filter.py": {"RecordIdFilter"},
}


FUNCTION_SOURCES = {
    "collection_json.py": {"collection_json_value", "collection_from_json_value"},
    "temporal_diff.py": {"diff_graph"},
    "catalog_copy.py": {"capture_copy", "prepare_copy_target", "copy_graph"},
    "workspace.py": {"resolve_workspace"},
    "html_snapshot.py": {"render_html_snapshot"},
    "sqlite_import.py": {"read_sqlite_rows", "import_sqlite"},
    "graph_interop.py": {"to_networkx", "projection_arrow_batches"},
    "polars.py": {"to_polars", "import_polars"},
    "text_import.py": {"read_csv_batches", "import_csv", "read_jsonl_batches", "import_jsonl"},
    "tabular.py": {"to_pandas", "import_pandas"},
    "parquet.py": {"read_parquet_batches", "import_parquet", "write_parquet"},
    "arrow.py": {"to_arrow_batches", "import_arrow_batches"},
    "projections.py": {"project_graph"},
    "migrations.py": {"migrate_schema"},
    "api/__init__.py": {"connect"},
    "backup.py": {"create_backup", "restore_backup"},
    "transfer.py": {"export_graph", "import_graph"},
}


def source_tree(relative: str) -> ast.Module:
    return ast.parse((ROOT / "src/okto_grafx" / relative).read_text(encoding="utf-8"))


def description(node: ast.AST) -> str:
    text = (
        ast.get_docstring(node) or "Returned data; see the owning operation."
    ).split("\n\n")[0]
    return " ".join(text.split()).replace("``", "`")


def methods(cls: ast.ClassDef) -> str:
    result = []
    for member in cls.body:
        if not isinstance(member, ast.FunctionDef) or member.name.startswith("_"):
            continue
        returns = ast.unparse(member.returns) if member.returns else "None"
        if any(
            isinstance(d, ast.Name) and d.id == "property"
            for d in member.decorator_list
        ):
            signature = f"{member.name}: {returns}  # read-only property"
        else:
            args = ast.unparse(member.args)
            args = args.removeprefix("self, ").removeprefix("cls, ")
            args = "" if args in ("self", "cls") else args
            signature = f"{member.name}({args}) -> {returns}"
        result.append(
            f"\n#### {cls.name}.{member.name}\n\n```python\n{signature}\n```\n\n{description(member)}\n"
        )
    return "".join(result)


def render() -> str:
    result = [
        MARKER,
        "\n\n## Complete facade signatures\n\n",
        "Generated from the public facade declarations; no engine instance is opened.\n",
        "Types in signatures are described below; `DEFAULT_QUERY_CURSOR_BATCH_ROWS` is 256.\n",
        "Factories, not direct engine constructors, own resource composition.\n",
    ]
    for cls in source_tree("engine/database.py").body:
        if isinstance(cls, ast.ClassDef) and cls.name in FACADES:
            result.append(f"\n### {cls.name}\n\n{description(cls)}\n" + methods(cls))
    for cls in source_tree("catalogs.py").body:
        if isinstance(cls, ast.ClassDef) and cls.name == "CatalogSession":
            result.append(f"\n### {cls.name}\n\n{description(cls)}\n" + methods(cls))
    for cls in source_tree("views.py").body:
        if isinstance(cls, ast.ClassDef) and cls.name == "LogicalViews":
            result.append(f"\n### {cls.name}\n\n{description(cls)}\n" + methods(cls))
    for cls in source_tree("domain/query/procedure_writer.py").body:
        if isinstance(cls, ast.ClassDef) and cls.name in ("ProcedureWriter", "ProcedureReader"):
            result.append(f"\n### {cls.name}\n\n{description(cls)}\n" + methods(cls))
    result.append("\n## Public factory and transfer functions\n\n")
    for relative, names in FUNCTION_SOURCES.items():
        module = "okto_grafx." + relative.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        for node in source_tree(relative).body:
            if isinstance(node, ast.FunctionDef) and node.name in names:
                result.append(f"\n### {module}.{node.name}\n\n```python\n{node.name}({ast.unparse(node.args)}) -> {ast.unparse(node.returns)}\n```\n\n{description(node)}\n")
    result.append(
        "\n## Result and observation type fields\n\n"
        "DTO module paths below are annotation/import locations, not permission to\n"
        "construct raw engine state. Consume returned instances and documented accessors.\n"
        "`Lsn`, `Csn` and record/table IDs are integer aliases, not wall-clock times.\n"
        "`RecordRef` is a physical page/slot identity, not your application primary key.\n"
        "`Value` is the detached value union described in the query-language reference.\n"
    )
    for relative, selected in DTO_SOURCES.items():
        for cls in source_tree(relative).body:
            if not isinstance(cls, ast.ClassDef) or cls.name.startswith("_"):
                continue
            if selected is not None and cls.name not in selected:
                continue
            fields = [
                n
                for n in cls.body
                if isinstance(n, ast.AnnAssign)
                and isinstance(n.target, ast.Name)
                and not n.target.id.startswith("_")
            ]
            if not fields:
                continue
            module = "okto_grafx." + relative.removesuffix(".py").replace("/", ".")
            result.append(
                f"\n### {cls.name} fields\n\nAnnotation location: `{module}.{cls.name}`.\n\n{description(cls)}\n\n```python\n"
            )
            for field in fields:
                result.append(f"{field.target.id}: {ast.unparse(field.annotation)}\n")
            result.append("```\n" + methods(cls))
    return "".join(result).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    text = TARGET.read_text(encoding="utf-8")
    prefix = text.split(MARKER, 1)[0].rstrip() + "\n\n"
    expected = prefix + render()
    if args.check:
        if text != expected:
            print(
                "Public API appendix is stale; run python tools/generate_api_reference.py"
            )
            return 1
    else:
        TARGET.write_text(expected, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
