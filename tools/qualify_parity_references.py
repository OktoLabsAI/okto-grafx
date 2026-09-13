"""Bounded FP-8 reference observations, not full vendor or TCK conformance.

Run using an isolated installed interpreter and its exact wheel. Each scenario
owns a fresh local database. No service startup, production path, overwrite,
installation, deletion, network operation or performance threshold is performed.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import date
from decimal import Decimal
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
import time
from zipfile import ZipFile


SCHEMA = ("CREATE NODE TABLE N(id INT64,v INT64,PRIMARY KEY(id))",
          "CREATE NODE TABLE M(id INT64,PRIMARY KEY(id))",
          "CREATE REL TABLE R(FROM N TO N)")
DATA = ("CREATE(:N {id:1,v:7}),(:N {id:2,v:7}),(:M {id:3})",
        "MATCH(a:N {id:1}),(b:N {id:2}) CREATE(a)-[:R]->(b),(a)-[:R]->(b),(b)-[:R]->(a)")


class ObservationError(Exception):
    """A harness/result-transport failure, never an engine semantic refusal."""


def scenarios():
    """Independent expected values; adaptations are explicit, never inferred from failures."""
    cases = [
        {"id": "numeric-syntax", "query": "RETURN 0x10 AS hex,0o10 AS octal,1 < 2 < 3 AS chained",
         "columns": ["hex", "octal", "chained"], "rows": [[16, 8, True]]},
        {"id": "nan-expression", "query": "RETURN (0.0/0.0) = (0.0/0.0) AS equal",
         "columns": ["equal"], "rows": [[False]]},
        {"id": "scalar-functions", "query": "RETURN lower('AB') AS lo,upper('ab') AS hi,trim(' x ') AS t,abs(-7) AS n",
         "columns": ["lo", "hi", "t", "n"], "rows": [["ab", "AB", "x", 7]]},
        {"id": "column-spelling", "query": "RETURN 1 + 2", "columns": ["1 + 2"], "rows": [[3]]},
        {"id": "entity-identity", "graph": True,
         "query": "MATCH(n:N),(m:N) WHERE n.v=m.v RETURN count(*) AS pairs,count(DISTINCT n) AS entities",
         "columns": ["pairs", "entities"], "rows": [[4, 2]]},
        {"id": "entity-union", "graph": True,
         "query": "CALL () { MATCH(n:N) RETURN n UNION MATCH(n:N) RETURN n } RETURN count(*) AS total",
         "columns": ["total"], "rows": [[2]]},
        {"id": "polymorphic-optional", "graph": True,
         "query": "MATCH(n) OPTIONAL MATCH(n)-[:R]->(m) RETURN n.id AS source,m.id AS target ORDER BY source,target",
         "columns": ["source", "target"], "rows": [[1, 2], [1, 2], [2, 1], [3, None]]},
        {"id": "bounded-trail", "graph": True,
         "query": "MATCH p=(a:N {id:1})-[:R*0..3]->(b) RETURN length(p) AS hops,b.id AS id ORDER BY hops,id",
         "columns": ["hops", "id"], "rows": [[0, 1], [1, 2], [1, 2], [2, 1], [2, 1], [3, 2], [3, 2]]},
        {"id": "multiple-labels", "graph": True,
         "query": "MATCH(n:N {id:1}) SET n:Extra REMOVE n:N RETURN labels(n) AS labels",
         "columns": ["labels"], "rows": [[["Extra"]]],
         "after": ("MATCH(n) RETURN n.id AS id ORDER BY id", ["id"], [[1], [2], [3]])},
        {"id": "date-value", "query": "RETURN date('2024-02-29') AS value",
         "columns": ["value"], "rows": [[date(2024, 2, 29)]]},
        {"id": "decimal-storage",
         "schema": ("CREATE NODE TABLE D(id INT64,amount DECIMAL(12,4),PRIMARY KEY(id))",),
         "setup": ("CREATE(:D {id:1,amount:decimal('1.2500',12,4)})",),
         "ladybug_setup": ("CREATE(:D {id:1,amount:CAST('1.2500' AS DECIMAL(12,4))})",),
         "adaptation": "Same declared DECIMAL(12,4); native decimal constructor versus Ladybug CAST, with no DOUBLE intermediate.",
         "query": "MATCH(n:D) RETURN n.amount AS value", "columns": ["value"], "rows": [[Decimal("1.2500")]]},
        {"id": "nested-storage",
         "schema": ("CREATE NODE TABLE Nest(id INT64,items LIST<STRUCT<num:INT64>>,PRIMARY KEY(id))",),
         "ladybug_schema": ("CREATE NODE TABLE Nest(id INT64,items STRUCT(num INT64)[],PRIMARY KEY(id))",),
         "adaptation": "Explicit native LIST<STRUCT> versus Ladybug STRUCT[] declaration; identical values/query.",
         "setup": ("CREATE(:Nest {id:1,items:[{num:1},{num:2}]})",),
         "query": "MATCH(n:Nest) RETURN n.items AS items", "columns": ["items"], "rows": [[[{"num": 1}, {"num": 2}]]]},
        {"id": "null-primary-key-atomicity", "graph": True,
         "query": "MATCH(n:N {id:1}) SET n.v=99,n.id=NULL RETURN n.id",
         "error_contains": "null",
         "after": ("MATCH(n:N) RETURN n.id AS id,n.v AS value ORDER BY id", ["id", "value"], [[1, 7], [2, 7]])},
    ]
    frozen = Path(__file__).resolve().parents[1] / "docs/conformance/EXTENSION_SCENARIOS_V1.json"
    for item in json.loads(frozen.read_text(encoding="utf-8"))["scenarios"]:
        if "query" in item:
            cases.append({"id": item["id"], "schema": tuple(item.get("setup", ())),
                          "query": item["query"], "columns": item["columns"], "rows": item["rows"],
                          **({"after": ("MATCH(n:T) RETURN n.id AS id ORDER BY id", ["id"], [[1], [2]])}
                             if item["id"] == "FP4-UNIT-CARDINALITY" else {})})
    return cases


def tagged(value):
    """Do not equate bool/int, stringify unknown values or round exact decimals."""
    if value is None:
        return {"type": "null"}
    if type(value) in (bool, int, float, str):
        return {"type": type(value).__name__, "value": value}
    if type(value) in (list, tuple):
        return {"type": "list", "value": [tagged(item) for item in value]}
    if type(value) is dict and all(type(key) is str for key in value):
        return {"type": "map", "value": {key: tagged(item) for key, item in sorted(value.items())}}
    if type(value) is Decimal or (type(value).__module__ == "okto_grafx.domain.model.decimal_values"
                                 and type(value).__name__ == "DecimalValue"):
        text = str(value) if type(value) is Decimal else value.to_string()
        return {"type": "decimal", "value": text}
    if type(value) is date or (type(value).__module__ == "okto_grafx.domain.model.temporal_values"
                              and type(value).__name__ == "DateValue"):
        return {"type": "date", "value": [value.year, value.month, value.day]}
    raise ObservationError(f"Unqualified reference result type: {type(value).__module__}.{type(value).__name__}")


def package_proof(module, wheel):
    origin = Path(module.__file__).resolve().parent
    assert origin.is_relative_to(Path(sys.prefix).resolve()), "Import escaped the selected installed environment"
    prefixes = (module.__name__ + "/", module.__name__ + ".libs/")
    with ZipFile(wheel) as archive:
        expected = {name: hashlib.sha256(archive.read(name)).hexdigest()
                    for name in archive.namelist() if name.startswith(prefixes) and not name.endswith("/")}
    roots = (origin, origin.with_name(module.__name__ + ".libs"))
    actual = {p.relative_to(origin.parent).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
              for root in roots for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts
              and p.suffix not in (".pyc", ".pyo")}
    assert expected and actual == expected, "Installed package differs from supplied wheel"
    return {"origin": str(origin), "wheel": str(wheel), "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "files": actual}


class Native:
    def __init__(self, engine, path):
        self.engine = engine
        if engine == "grafx":
            from okto_grafx import connect
            self.db = connect(path)
            self.db.ensure_identity_indexes()
            self.conn = None
        else:
            import ladybug
            self.db = ladybug.Database(str(path), buffer_pool_size=128 * 1024**2,
                                      max_num_threads=2, max_db_size=1024**3)
            self.conn = ladybug.Connection(self.db)

    def execute(self, query):
        if self.engine == "grafx":
            with self.db.begin("write") as tx:
                result = tx.execute(query)
                return {"columns": list(result.columns), "rows": tagged(result.rows)}
        result = self.conn.execute(query)
        try:
            rows = []
            while result.has_next():
                rows.append(result.get_next())
            return {"columns": result.get_column_names(), "rows": tagged(rows),
                    "declared_types": result.get_column_data_types()}
        finally:
            result.close()

    def close(self):
        if self.conn is not None:
            self.conn.close()
        self.db.close()


def matches(result, columns, rows):
    return result.get("columns") == list(columns) and result.get("rows") == tagged(rows)


def observe(engine, root, case):
    result = {"id": case["id"], "query": case["query"], "adaptation": case.get("adaptation"),
              "schema": list(case.get(engine + "_schema", case.get("schema", SCHEMA if case.get("graph") else ()))),
              "setup": list(case.get(engine + "_setup", case.get("setup", DATA if case.get("graph") else ()))),
              "expected": ({"error_contains": case["error_contains"]} if "error_contains" in case else
                           {"columns": case["columns"], "rows": tagged(case["rows"])})}
    start = time.perf_counter()
    db = None
    phase = "open"
    try:
        db = Native(engine, root / case["id"])
        phase = "setup"
        for statement in result["schema"] + result["setup"]:
            db.execute(statement)
        phase = "query"
        try:
            result["observed"] = db.execute(case["query"])
            equal = "error_contains" not in case and matches(result["observed"], case["columns"], case["rows"])
        except ObservationError:
            raise
        except Exception as exc:
            result["error"] = {"type": type(exc).__name__, "message": str(exc),
                               "code": getattr(exc, "code", None), "details": dict(getattr(exc, "details", {}))}
            equal = "error_contains" in case and case["error_contains"] in str(exc).lower()
        if "after" in case:
            phase = "effects"
            query, columns, rows = case["after"]
            result["after_query"] = query
            result["after_expected"] = {"columns": columns, "rows": tagged(rows)}
            result["after"] = db.execute(query)
            equal = equal and matches(result["after"], columns, rows)
        result["comparison"] = "matches" if equal else "differs"
    except Exception as exc:
        result.update(comparison="unavailable", failed_phase=phase,
                      failure={"type": type(exc).__name__, "message": str(exc)})
    finally:
        if db is not None:
            try:
                db.close()
            except Exception as exc:
                result.update(comparison="unavailable", failed_phase="close",
                              failure={"type": type(exc).__name__, "message": str(exc)})
        result["seconds"] = round(time.perf_counter() - start, 6)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("grafx", "ladybug"), required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    module = __import__("okto_grafx" if args.engine == "grafx" else "ladybug")
    observed_version = version("okto-grafx" if args.engine == "grafx" else "ladybug")
    if args.engine == "ladybug":
        assert observed_version == "0.20.3", "Reference version differs from frozen FP-1 selection"
    report = {"engine": args.engine, "version": observed_version, "python": sys.version,
              "package": package_proof(module, wheel), "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "scope": "Bounded supplemental reference observations; not full TCK/vendor conformance or a performance gate",
              "cases": []}
    for case in scenarios():
        result = observe(args.engine, output, case)
        report["cases"].append(result)
        report["summary"] = dict(Counter(item["comparison"] for item in report["cases"]))
        (output / "report.json").write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
        print(case["id"], result["comparison"], flush=True)
    # A reference difference is a comparative result, never a Grafx waiver.
    return int(any(c["comparison"] != "matches" for c in report["cases"])) if args.engine == "grafx" else int(
        any(c["comparison"] == "unavailable" for c in report["cases"]))


if __name__ == "__main__":
    raise SystemExit(main())
