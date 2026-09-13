"""Bounded pinned Neo4j observations; requires an explicitly owned test container.

Preparation never starts a service. Execution refuses arbitrary database URLs,
host bind mounts, unpinned images, non-loopback ports and unowned containers.
Queries/expected answers come unchanged from the shared reference scenario set.
Only the schema dialect is adapted, with differences recorded before execution.
"""

from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import importlib.util
from importlib.metadata import version
import json
from pathlib import Path
import re
import subprocess
import time


ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "tools/qualify_parity_references.py"
PIN_FILE = ROOT / "docs/conformance/REFERENCE_VERSIONS_V1.json"
PINS = json.loads(PIN_FILE.read_text(encoding="utf-8"))
SPEC = importlib.util.spec_from_file_location("fp_shared_reference", SHARED)
shared = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(shared)
IMAGE = "neo4j@" + PINS["neo4j"]["linux_amd64_digest"]
LABEL = "com.okto.grafx.fp-reference"
PORT = "17687"


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, allow_nan=False)


def normalized_case(case):
    """Preserve the shared query/value/effect oracle and expose weaker CE schema."""
    schemas = list(case.get("schema", shared.SCHEMA if case.get("graph") else ()))
    schema = []
    notes = []
    for statement in schemas:
        match = re.fullmatch(r"CREATE NODE TABLE ([A-Za-z][A-Za-z0-9]*)(\(.*\))", statement)
        if match and "PRIMARYKEY(id)" in statement.replace(" ", ""):
            label = match.group(1)
            schema.append(f"CREATE CONSTRAINT fp_{label}_id FOR (n:{label}) REQUIRE n.id IS UNIQUE")
            notes.append("Typed node table " + label + " becomes a label with UNIQUE(id). "
                         "CE uniqueness does not enforce property existence or declared column types; "
                         "this is not equivalent to PRIMARY KEY/DECIMAL/LIST<STRUCT> admission.")
        elif statement == "CREATE REL TABLE R(FROM N TO N)":
            notes.append("Relationship R uses native dynamic type creation; no endpoint schema constraint.")
        else:
            raise AssertionError("Unreviewed schema adaptation: " + statement)
    result = {"id": case["id"], "query": case["query"], "original_schema": schemas,
              "schema": schema,
              "setup": list(case.get("setup", shared.DATA if case.get("graph") else ())),
              "adaptations": notes,
              "expected": ({"error_contains": case["error_contains"]} if "error_contains" in case else
                           {"columns": case["columns"], "rows": shared.tagged(case["rows"])})}
    if "after" in case:
        query, columns, rows = case["after"]
        result.update(after_query=query, after_expected={"columns": columns, "rows": shared.tagged(rows)})
    if case["id"] in ("decimal-storage", "nested-storage"):
        result["adaptations"].append("No DOUBLE/string/list flattening substitution. Execute the original "
                                      "CREATE and record a semantic refusal, including setup phase, as a difference.")
    return result


def manifest():
    return {"status": "prepared_not_executed", "engine": "Neo4j Community", "version": "5.26.0",
            "image": IMAGE, "pins_sha256": digest(PIN_FILE), "shared_tool_sha256": digest(SHARED),
            "extension_sha256": digest(ROOT / "docs/conformance/EXTENSION_SCENARIOS_V1.json"),
            "transport": "Private Python driver 5.26.0; explicit single-statement transactions, no automatic retries",
            "isolation": "Owned disposable container, no host bind mounts, only 127.0.0.1:17687; "
                         "empty nodes/edges/constraints between cases, reused server/token catalog",
            "comparison_policy": "Unchanged expected answers. Semantic query/setup refusals differ; "
                                 "transport, authorization, server and harness failures are unavailable. "
                                 "A differing CE constraint contract is not an atomicity defect.",
            "cases": [normalized_case(case) for case in shared.scenarios()]}


def docker_json(*arguments):
    result = subprocess.run(["docker", *arguments], check=True, capture_output=True, text=True, timeout=30)
    return json.loads(result.stdout)


def guard_container(container_id, run_id):
    """Refuse data operations unless the exact isolated container is still owned."""
    assert re.fullmatch(r"[a-f0-9]{64}", container_id), "Require full immutable container ID"
    assert re.fullmatch(r"fp-[a-f0-9]{32}", run_id), "Require per-run ownership identity"
    container, = docker_json("inspect", container_id)
    assert container["Id"] == container_id and container["State"]["Running"]
    assert container["Config"]["Labels"].get(LABEL) == run_id
    assert container["Config"]["Image"] == IMAGE
    assert "NEO4J_AUTH=none" in container["Config"]["Env"]
    assert not any(mount["Type"] == "bind" for mount in container["Mounts"])
    assert container["HostConfig"]["NetworkMode"] == "bridge"
    ports = {key: value for key, value in container["NetworkSettings"]["Ports"].items() if value}
    assert ports == {"7687/tcp": [{"HostIp": "127.0.0.1", "HostPort": PORT}]}
    image, = docker_json("image", "inspect", container["Image"])
    assert image["Os"] == "linux" and image["Architecture"] == "amd64"
    assert any(item.endswith("@" + PINS["neo4j"]["linux_amd64_digest"])
               for item in image["RepoDigests"])
    return {"container_id": container_id, "run_id": run_id, "created": container["Created"],
            "image_id": image["Id"], "repo_digests": image["RepoDigests"],
            "mounts": container["Mounts"], "published_ports": ports,
            "memory_limit": container["HostConfig"]["Memory"],
            "nano_cpus": container["HostConfig"]["NanoCpus"]}


def tagged(value):
    """Observe driver-native DATE rather than treating a string as a typed value."""
    from neo4j.time import Date
    if type(value) is Date:
        return {"type": "date", "value": [value.year, value.month, value.day]}
    if type(value) in (list, tuple):
        return {"type": "list", "value": [tagged(item) for item in value]}
    if type(value) is dict and all(type(key) is str for key in value):
        return {"type": "map", "value": {key: tagged(item) for key, item in sorted(value.items())}}
    return shared.tagged(value)


def execute(driver, query):
    """Consume before explicit commit, preserving columns, duplicate rows and types."""
    with driver.session(database="neo4j", fetch_size=100) as session:
        with session.begin_transaction(timeout=30) as tx:
            result = tx.run(query)
            columns = list(result.keys())
            rows = [record.values() for record in result]
            summary = result.consume()
            observed = {"columns": columns, "rows": tagged(rows),
                        "counters": {name: getattr(summary.counters, name) for name in
                                     ("nodes_created", "nodes_deleted", "relationships_created",
                                      "relationships_deleted", "properties_set", "labels_added", "labels_removed")}}
            tx.commit()
            return observed


def equal(observed, expected):
    return all(observed.get(key) == expected[key] for key in ("columns", "rows"))


def semantic_error(exc):
    """Do not disguise unavailable procedures/services, auth or OOM as parity differences."""
    from neo4j.exceptions import Neo4jError
    return (isinstance(exc, Neo4jError) and isinstance(exc.code, str)
            and exc.code.startswith(("Neo.ClientError.Statement.", "Neo.ClientError.Schema.")))


def clean_fixture(driver, container_id, run_id, *, first=False):
    guard_container(container_id, run_id)
    if not first:
        execute(driver, "MATCH(n) DETACH DELETE n")
        # All names are drawn from the predeclared schema, never server/user text.
        for name in ("N", "M", "D", "Nest", "T"):
            execute(driver, f"DROP CONSTRAINT fp_{name}_id IF EXISTS")
    nodes = execute(driver, "MATCH(n) RETURN count(n) AS total")
    edges = execute(driver, "MATCH()-[r]->() RETURN count(r) AS total")
    constraints = execute(driver, "SHOW CONSTRAINTS YIELD name RETURN name")
    assert equal(nodes, {"columns": ["total"], "rows": tagged([[0]])})
    assert equal(edges, {"columns": ["total"], "rows": tagged([[0]])})
    assert equal(constraints, {"columns": ["name"], "rows": tagged([])})


def observe(driver, case):
    result = dict(case)
    phase = "schema"
    start = time.perf_counter()
    try:
        for statement in case["schema"]:
            execute(driver, statement)
        phase = "setup"
        for statement in case["setup"]:
            execute(driver, statement)
        phase = "query"
        try:
            observed = execute(driver, case["query"])
            result["observed"] = observed
            matches = "error_contains" not in case["expected"] and equal(observed, case["expected"])
        except Exception as exc:
            if not semantic_error(exc):
                raise
            result["error"] = {"type": type(exc).__name__, "code": exc.code, "message": str(exc)}
            matches = ("error_contains" in case["expected"] and
                       case["expected"]["error_contains"] in str(exc).lower())
        if "after_query" in case:
            phase = "effects"
            result["after"] = execute(driver, case["after_query"])
            matches = matches and equal(result["after"], case["after_expected"])
        result["comparison"] = "matches" if matches else "differs"
    except Exception as exc:
        result.update(comparison="differs" if phase in ("schema", "setup") and semantic_error(exc)
                      else "unavailable", failed_phase=phase,
                      failure={"type": type(exc).__name__, "code": getattr(exc, "code", None),
                               "message": str(exc)})
        if result["comparison"] == "differs":
            # Record effects after a rejected setup, not a fabricated successful result.
            result["setup_nodes"] = execute(driver, "MATCH(n) RETURN count(n) AS total")
    result["seconds"] = round(time.perf_counter() - start, 6)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "run"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--container-id")
    parser.add_argument("--run-id")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--driver-wheel", type=Path)
    args = parser.parse_args()
    if args.mode == "prepare":
        write_new(args.output, manifest())
        print("PREPARED", len(manifest()["cases"]), "scenarios", digest(args.output), flush=True)
        return 0
    assert args.container_id and args.run_id and args.manifest and args.driver_wheel
    declared = json.loads(args.manifest.read_text(encoding="utf-8"))
    assert declared == manifest(), "Prepared scenario/dialect declaration changed"
    ownership = guard_container(args.container_id, args.run_id)
    import neo4j
    assert version("neo4j") == "5.26.0"
    report = {"engine": "Neo4j Community", "manifest": str(args.manifest),
              "manifest_sha256": digest(args.manifest), "runner_sha256": digest(__file__),
              "driver": shared.package_proof(neo4j, args.driver_wheel.resolve()),
              "container": ownership, "status": "running", "cases": []}
    write_new(args.output, report)
    with neo4j.GraphDatabase.driver("bolt://127.0.0.1:" + PORT, auth=None, connection_timeout=10,
                                    max_transaction_retry_time=0) as driver:
        report["server"] = execute(driver, "CALL dbms.components() YIELD versions,edition RETURN versions,edition")
        assert equal(report["server"], {"columns": ["versions", "edition"],
                                        "rows": tagged([[["5.26.0"], "community"]])})
        clean_fixture(driver, args.container_id, args.run_id, first=True)
        for case in declared["cases"]:
            result = observe(driver, case)
            report["cases"].append(result)
            clean_fixture(driver, args.container_id, args.run_id)
            report["summary"] = dict(Counter(item["comparison"] for item in report["cases"]))
            args.output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
            print(case["id"], result["comparison"], flush=True)
    assert guard_container(args.container_id, args.run_id)["image_id"] == ownership["image_id"]
    report["status"] = "terminal"
    report["driver_after"] = shared.package_proof(neo4j, args.driver_wheel.resolve())
    assert report["driver_after"] == report["driver"]
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    return int(any(case["comparison"] == "unavailable" for case in report["cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
