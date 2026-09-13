"""Inventory the pinned upstream TCK without mistaking parse acceptance for conformance.

The upstream files remain in their licensed checkout. Cucumber's compiler expands
Scenario Outlines and Backgrounds; no scenario is silently discarded by a regex.
The JSON includes every executable case and every query step, including setup.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Protocol

from gherkin import Compiler, Parser

from okto_grafx.domain.errors import GrafxError
from okto_grafx.domain.query.parser import parse

UPSTREAM_REVISION = "677cbafabb8c3c5eed458fd3b1ec0daec8d67d23"
UPSTREAM_TAG = "2024.3"


class ReadExecutor(Protocol):
    """The public transaction surface needed by read-only reference scenarios."""

    def execute(self, text: str, parameters: dict[str, object]) -> object:
        """Execute a query and return the public columns/rows result."""
        ...


class _TCKLiterals(ast.NodeTransformer):
    """Translate only literal names, without executing expressions from the corpus."""

    def visit_Name(self, node: ast.Name) -> ast.AST:
        """Translate the three Cypher literal names; reject all other identifiers."""
        values = {"null": None, "true": True, "false": False}
        if node.id not in values:
            raise ValueError(f"Unsupported TCK value name: {node.id}")
        return ast.copy_location(ast.Constant(values[node.id]), node)

    def visit_Dict(self, node: ast.Dict) -> ast.AST:
        """Treat bare map keys as strings, while continuing to reject executable values."""
        node.keys = [ast.copy_location(ast.Constant(key.id), key) if isinstance(key, ast.Name)
                     else key for key in node.keys]
        return self.generic_visit(node)


def literal_value(text: str) -> object:
    """Read scalar/list/map TCK cells without Python eval or Grafx as its own oracle."""
    return ast.literal_eval(_TCKLiterals().visit(ast.parse(text, mode="eval")))


def canonical_value(value: object) -> object:
    """Compare public results with type-aware, hashable recursive values."""
    if isinstance(value, (tuple, list)):
        return ("list", tuple(canonical_value(item) for item in value))
    if isinstance(value, dict):
        return ("map", tuple(sorted((key, canonical_value(item)) for key, item in value.items())))
    return (type(value).__name__, value)


def run_read_case(case: dict[str, object], executor: ReadExecutor) -> dict[str, object]:
    """Run supported read-only TCK steps; unknown fixtures/errors remain explicitly unrun.

    Validate the complete scenario before issuing any query. Future fixture/write
    adapters must provide their own side-effect verification before being admitted.
    """
    query: str | None = None
    expected: list[tuple[object, ...]] | None = None
    columns: tuple[str, ...] = ()
    parameters: dict[str, object] = {}
    ordered = False
    no_effects = False
    try:
        for step in case["steps"]:
            name = step["text"]
            argument = step.get("argument", {})
            if name in ("any graph", "an empty graph"):
                continue
            if name == "parameters are:":
                for row in argument["dataTable"]["rows"]:
                    key, value = (cell["value"] for cell in row["cells"])
                    parameters[key] = literal_value(value)
            elif name == "executing query:":
                if query is not None:
                    raise ValueError("Multiple query steps need a stateful scenario adapter")
                query = argument["docString"]["content"]
            elif name in ("the result should be, in any order:", "the result should be, in order:"):
                ordered = name.endswith("in order:")
                table = argument["dataTable"]["rows"]
                columns = tuple(cell["value"] for cell in table[0]["cells"])
                expected = [tuple(literal_value(cell["value"]) for cell in row["cells"])
                            for row in table[1:]]
            elif name == "no side effects":
                no_effects = True
            else:
                raise ValueError(f"Scenario adapter not implemented for step: {name}")
        if query is None or expected is None or not no_effects:
            raise ValueError("Scenario needs a query, expected rows and verified zero side effects")
    except (ValueError, SyntaxError) as exc:
        return {"conformance": "not_run", "reason": str(exc)}
    try:
        # The supplied executor MUST be a read transaction, so a write cannot
        # satisfy an upstream no-side-effects assertion by being silently committed.
        result = executor.execute(query, parameters)
        actual = [canonical_value(tuple(row)) for row in result.rows]
        wanted = [canonical_value(row) for row in expected]
        if tuple(result.columns) != columns:
            raise AssertionError(f"Columns differ: {result.columns!r} != {columns!r}")
        if (actual != wanted) if ordered else (Counter(actual) != Counter(wanted)):
            raise AssertionError(f"Rows differ: {result.rows!r} != {expected!r}")
    except (GrafxError, AssertionError) as exc:
        return {"conformance": "failed", "reason": str(exc)}
    return {"conformance": "passed", "reason": None}


def compile_feature(source: str, uri: str) -> list[dict[str, object]]:
    """Expand one unchanged upstream feature into stable, individually tracked cases."""
    document = Parser().parse(source)
    document["uri"] = uri
    cases: list[dict[str, object]] = []
    for ordinal, pickle in enumerate(Compiler().compile(document), start=1):
        queries: list[dict[str, object]] = []
        for step in pickle["steps"]:
            argument = step.get("argument", {})
            doc = argument.get("docString")
            if doc is None or step["text"] not in (
                "having executed:", "executing query:", "executing control query:",
            ):
                continue
            query = doc["content"]
            try:
                parse(query)
            except GrafxError as exc:
                parse_status = "refused"
                reason = str(exc)
            else:
                parse_status = "accepted"
                reason = None
            queries.append({"step": step["text"], "query": query,
                            "parse_status": parse_status, "reason": reason})
        cases.append({"id": f"{uri}#{ordinal:04d}", "name": pickle["name"],
                      "steps": pickle["steps"], "queries": queries,
                      "conformance": "not_run"})
    return cases


def inventory(checkout: Path) -> dict[str, object]:
    """Verify the exact upstream revision and enumerate every expanded TCK case."""
    revision = subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if revision != UPSTREAM_REVISION:
        raise ValueError(f"TCK revision mismatch: {revision}; need {UPSTREAM_REVISION}")
    dirty = subprocess.run(
        ["git", "-C", str(checkout), "status", "--porcelain", "--", "tck/features", "tck/graphs"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if dirty:
        raise ValueError("TCK feature checkout is modified; use unchanged upstream scenarios")
    root = checkout / "tck" / "features"
    paths = sorted(root.rglob("*.feature"))
    if not paths:
        raise ValueError("The upstream checkout contains no TCK feature files")
    cases: list[dict[str, object]] = []
    hashes: dict[str, str] = {}
    for path in paths:
        uri = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        hashes[uri] = hashlib.sha256(source.encode("utf-8")).hexdigest()
        cases.extend(compile_feature(source, uri))
    graph_fixtures = {}
    for path in sorted((checkout / "tck" / "graphs").rglob("*.cypher")):
        content = path.read_text(encoding="utf-8")
        graph_fixtures[path.stem] = {
            "uri": path.relative_to(checkout).as_posix(),
            "sha256_lf": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "query": content,
        }
    return {"schema_version": 1, "upstream_tag": UPSTREAM_TAG,
            "upstream_revision": revision, "feature_count": len(paths),
            "case_count": len(cases), "feature_sha256_lf": hashes,
            "graph_fixtures": graph_fixtures,
            "warning": "Parse acceptance is not execution or semantic conformance.",
            "cases": cases}


def main() -> int:
    """Write a reproducible inventory; never count unexecuted cases as passes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute-reads", action="store_true",
                        help="Execute supported positive read scenarios against an empty public database")
    parser.add_argument("--execute-stateful", action="store_true",
                        help="Use isolated native transactions, graph observations and durable reopen")
    parser.add_argument("--infer-fixture-schema", action="store_true",
                        help="Record typed single-label fixture schema adaptation; never rewrite tested queries")
    parser.add_argument("--feature-prefix", default="", help="Restrict execution, not inventory coverage")
    parser.add_argument("--owner", choices=[f"FP-{number}" for number in range(1, 9)],
                        help="Restrict execution to an owner in --verify-ledger, retaining the full inventory")
    parser.add_argument("--ledger-output", type=Path,
                        help="Create all-case ownership/expectation ledger; does not freeze exclusions")
    parser.add_argument("--verify-ledger", type=Path,
                        help="Refuse changed/missing source cases before any execution")
    parser.add_argument("--predecessor-ledger", type=Path,
                        help="Frozen immediate predecessor (V1 for V2; V2 for V3)")
    parser.add_argument("--ancestor-ledger", type=Path,
                        help="Frozen V1 source required alongside V2 when verifying V3")
    args = parser.parse_args()
    if args.execute_reads and args.execute_stateful:
        parser.error("Choose either read-only diagnostics or stateful execution")
    if args.infer_fixture_schema and not args.execute_stateful:
        parser.error("--infer-fixture-schema requires --execute-stateful")
    if args.owner and not args.verify_ledger:
        parser.error("--owner requires --verify-ledger")
    if args.predecessor_ledger and not args.verify_ledger:
        parser.error("--predecessor-ledger requires --verify-ledger")
    if args.ancestor_ledger and not args.predecessor_ledger:
        parser.error("--ancestor-ledger requires --predecessor-ledger")
    for target in (args.output, args.ledger_output):
        if target is not None and target.exists():
            saved = json.loads(target.read_text(encoding="utf-8"))
            if saved.get("status", "").startswith("frozen_"):
                parser.error("Refusing to overwrite a frozen ledger with a report or draft")
    report = inventory(args.checkout)
    # Also support direct script execution (tools/ is then sys.path[0]).
    if args.ledger_output or args.verify_ledger:
        if __package__:
            from tools.tck_ledger import build_ledger, profile_summary, verify_ledger
        else:
            from tck_ledger import build_ledger, profile_summary, verify_ledger

        if args.verify_ledger:
            ledger = json.loads(args.verify_ledger.read_text(encoding="utf-8"))
            predecessor = (json.loads(args.predecessor_ledger.read_text(encoding="utf-8"))
                           if args.predecessor_ledger else None)
            ancestor = (json.loads(args.ancestor_ledger.read_text(encoding="utf-8"))
                        if args.ancestor_ledger else None)
            verify_ledger(ledger, report, predecessor=predecessor, ancestor=ancestor)
    selected_ids = {case["id"] for case in report["cases"]
                    if case["id"].startswith(args.feature_prefix)}
    if args.owner:
        selected_ids &= {case["id"] for case in ledger["cases"] if case["owner"] == args.owner}
    report["execution_selection"] = {"feature_prefix": args.feature_prefix,
                                     "owner": args.owner, "case_count": len(selected_ids)}
    if args.execute_reads:
        import okto_grafx

        with tempfile.TemporaryDirectory(prefix="grafx-tck-") as temporary:
            with okto_grafx.connect(Path(temporary) / "db") as database:
                for case in report["cases"]:
                    if case["id"] in selected_ids:
                        with database.begin("read") as transaction:
                            case.update(run_read_case(case, transaction))
        report["execution_summary"] = dict(Counter(case["conformance"] for case in report["cases"]))
    if args.execute_stateful:
        if __package__:
            from tools.tck_native import NativeScenarioBackend
            from tools.tck_stateful import run_stateful_case
        else:
            from tck_native import NativeScenarioBackend
            from tck_stateful import run_stateful_case
        for case in report["cases"]:
            if case["id"] in selected_ids:
                backend = NativeScenarioBackend(infer_schema=args.infer_fixture_schema,
                                                graph_fixtures=report["graph_fixtures"])
                try:
                    case.update(run_stateful_case(case, backend))
                finally:
                    backend.close()
        report["execution_summary"] = dict(Counter(case["conformance"] for case in report["cases"]))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.verify_ledger:
        report["profile_summary"] = profile_summary(report, ledger, predecessor=predecessor, ancestor=ancestor)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.ledger_output:
        args.ledger_output.parent.mkdir(parents=True, exist_ok=True)
        args.ledger_output.write_text(json.dumps(build_ledger(report), indent=2, ensure_ascii=False)
                                      + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("upstream_revision", "feature_count", "case_count")}))
    if "execution_summary" in report:
        print(json.dumps(report["execution_summary"]))
    return int(report.get("execution_summary", {}).get("failed", 0) > 0)


if __name__ == "__main__":
    raise SystemExit(main())
