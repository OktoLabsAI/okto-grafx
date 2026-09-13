"""Bounded offline checks for the consumer docs and consolidated roadmap archive."""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from generate_api_reference import MARKER, render  # noqa: E402

CONSUMER_DOCS = (
    "docs/reports/FP_FINAL_NATIVE_QUALIFICATION.md",
    "docs/reports/FP_FINAL_PULSE_QUALIFICATION.md",
    "docs/reports/FP_NATIVE_COST_OBSERVATIONS.md",
    "docs/conformance/EXTENSION_COVERAGE.md",
    "docs/conformance/README.md",
    "docs/specs/FUNCTIONAL_PARITY_PLAN.md",
    "docs/reports/FP_INTEGRATED_REGRESSION_CORRECTIONS.md",
    "docs/TYPE_SUPPORT.md",
    "docs/ENTITY_VALUES.md",
    "docs/TEMPORAL_VALUES.md",
    "docs/CYPHER_COMPATIBILITY.md",
    "docs/COMPOSABLE_QUERIES.md",
    "docs/FEATURE_COMPARISON.md",
    "docs/reports/V006_NHC_ROUND.md",
    "docs/SYSTEM_TIME_HISTORY.md",
    "docs/V006_COMPATIBILITY.md",
    "docs/CATALOG_COPY.md",
    "docs/CATALOGS_AND_WORKSPACES.md",
    "docs/POSTING_HASH.md",
    "docs/specs/SYSTEM_HISTORY_V1.md",
    "docs/specs/FTS_POSITIONAL_POSTINGS.md",
    "docs/reports/V006_NATIVE_HISTORY_ROUND.md",
    "docs/GRAPH_PROJECTIONS.md",
    "docs/TABULAR_AND_PARQUET.md",
    "docs/GRAPH_EXCHANGE.md",
    "docs/LOCAL_TEXT_IMPORT.md",
    "docs/POLARS_RECIPE.md",
    "docs/EXTENSIONS_AND_ARROW.md",
    "docs/V005_COMPATIBILITY.md",
    "docs/specs/HEAP_FREE_PAGE_INDEX.md",
    "docs/specs/SPARSE_HASH_DIRECTORIES.md",
    "README.md",
    "ROADMAP.md",
    "docs/README.md",
    "docs/GETTING_STARTED.md",
    "docs/INTEGRATION.md",
    "docs/API_REFERENCE.md",
    "docs/CONFIGURATION.md",
    "docs/QUERY_LANGUAGE.md",
    "docs/INDEXES_AND_VECTORS.md",
    "docs/OPERATIONS.md",
    "docs/CLI.md",
    "docs/PERFORMANCE.md",
    "docs/FULL_TEXT_SEARCH.md",
    "docs/HYBRID_SEARCH.md",
    "docs/SCHEMA_MIGRATIONS.md",
    "docs/BACKUP_RESTORE.md",
    "docs/specs/FTS_DURABLE_STATISTICS.md",
    "docs/specs/LARGE_HASH_DIRECTORIES.md",
    "docs/LOGICAL_TRANSFER.md",
    "docs/specs/FULLTEXT_V1_FORMAT.md",
    "docs/reports/README.md",
)


def headings(text: str) -> set[str]:
    """GitHub-like anchors for authored headings (ignore fenced code)."""
    anchors = set(re.findall(r'<a id="([^"]+)"', text))
    seen: dict[str, int] = {}
    for line in re.sub(r"```.*?```", "", text, flags=re.S).splitlines():
        match = re.match(r"^#{1,6}\s+(.+?)\s*#*$", line)
        if not match:
            continue
        slug = re.sub(r"[^\w\- ]", "", match[1].lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        anchors.add(slug if not count else f"{slug}-{count}")
    return anchors


def check() -> list[str]:
    errors: list[str] = []
    for name in CONSUMER_DOCS:
        path = ROOT / name
        if not path.exists():
            errors.append(f"Missing consumer doc: {name}")
            continue
        text = path.read_text(encoding="utf-8")
        for target in re.findall(r"(?<!!)\[[^\]\n]+\]\(([^\s()]+)\)", text):
            if re.match(r"[a-zA-Z][\w+.-]*:", target):
                continue
            relative, _, fragment = unquote(target).partition("#")
            destination = (path.parent / relative).resolve() if relative else path
            if not destination.exists():
                errors.append(f"{name}: broken link {target}")
            elif fragment and destination.suffix == ".md":
                if fragment not in headings(destination.read_text(encoding="utf-8")):
                    errors.append(f"{name}: missing anchor {target}")

    config_tree = ast.parse(
        (ROOT / "src/okto_grafx/runtime/config.py").read_text(encoding="utf-8")
    )
    config = next(
        n
        for n in config_tree.body
        if isinstance(n, ast.ClassDef) and n.name == "DatabaseConfig"
    )
    fields = {
        n.target.id
        for n in config.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
    }
    documented = set(
        re.findall(
            r"^\| `([a-z_]+)` \|",
            (ROOT / "docs/CONFIGURATION.md").read_text(encoding="utf-8"),
            re.M,
        )
    )
    if fields != documented:
        errors.append(
            f"Config field drift: missing={sorted(fields - documented)}, extra={sorted(documented - fields)}"
        )

    for module, classes, guide in (
        ("domain/query/hybrid.py", {"HybridSearchOptions"}, "HYBRID_SEARCH.md"),
        ("domain/index/fulltext.py", {"TextIndexOptions", "TextSearchLimits"}, "FULL_TEXT_SEARCH.md"),
        ("transfer.py", {"TransferLimits"}, "LOGICAL_TRANSFER.md"),
        ("projections.py", {"ProjectionLimits"}, "GRAPH_PROJECTIONS.md"),
        ("text_import.py", {"TextImportLimits"}, "LOCAL_TEXT_IMPORT.md"),
    ):
        tree = ast.parse((ROOT / "src/okto_grafx" / module).read_text(encoding="utf-8"))
        expected = {
            field.target.id for cls in tree.body if isinstance(cls, ast.ClassDef) and cls.name in classes
            for field in cls.body if isinstance(field, ast.AnnAssign) and isinstance(field.target, ast.Name)
        }
        found = set(re.findall(r"^\| `([a-z_]+)` \|", (ROOT / "docs" / guide).read_text(encoding="utf-8"), re.M))
        if expected != found:
            errors.append(f"{guide}: option drift: missing={sorted(expected - found)}, extra={sorted(found - expected)}")

    api = (ROOT / "docs/API_REFERENCE.md").read_text(encoding="utf-8")
    if MARKER not in api or api.split(MARKER, 1)[1] != render().split(MARKER, 1)[1]:
        errors.append("Public signature/DTO appendix stale; regenerate it")

    archive = (ROOT / "docs/archive/ROADMAP_SOURCES.md").read_text(encoding="utf-8")
    manifest = json.loads(
        (ROOT / "docs/archive/ROADMAP_SOURCES_MANIFEST.json").read_text(
            encoding="utf-8"
        )
    )
    for entry in manifest:
        source = entry["source"]
        begin, end = (
            f"<!-- BEGIN SOURCE {source} -->\n",
            f"\n<!-- END SOURCE {source} -->",
        )
        if archive.count(begin) != 1 or archive.count(end) != 1:
            errors.append(f"Archive boundary missing/duplicate: {source}")
            continue
        body = archive.split(begin, 1)[1].split(end, 1)[0]
        if hashlib.sha256(body.encode()).hexdigest() != entry["archived_sha256_lf"]:
            errors.append(f"Archived source changed: {source}")
        if (
            len(body.splitlines()) != entry["original_lines"]
            or entry["original_lines"] != entry["archived_lines"]
        ):
            errors.append(f"Archived source line count changed: {source}")
        if (ROOT / source).exists():
            errors.append(f"Competing original plan restored: {source}")
    return errors


if __name__ == "__main__":
    failures = check()
    print(
        "\n".join(failures)
        if failures
        else "Documentation PASS: links/anchors, 39 config fields, public signatures/DTOs, 11 preserved source plans"
    )
    raise SystemExit(bool(failures))
