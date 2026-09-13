"""Qualify logical transfer across explicitly installed old/current wheels.

Workers use isolated interpreters and prove installed import origins. No package
installation, existing output directory, production database or deletion is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def snapshot(root):
    """Audit every file and directory, including empty workspaces and control files."""
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
            for p in sorted(root.rglob("*"))}


def validate_refusal(result, *, shape="plain"):
    """Require the exact archived format/type boundary, not an unrelated failure."""
    refusal = result["refusal"]
    if shape in ("decimal", "typed_collections", "type_bundle"):
        if result.get("resume") is True:
            # This archived importer leaks the exact schema-admission exception
            # before opening any workspace. Record it, never call it a typed error.
            assert refusal["code"] is None and refusal["details"] == {}, refusal
        else:
            assert refusal["code"] == "recovery_refused", refusal
            assert refusal["details"].get("operation") == "logical_transfer", refusal
            assert refusal["details"].get("reason") == "artifact_invalid", refusal
        cause = refusal["cause"]
        if shape == "typed_collections":
            assert cause == {"type": "TypeError", "message":
                             "ColumnDef.__init__() got an unexpected keyword argument 'stored_type'"}, cause
        else:
            assert cause == {"type": "KeyError", "message": "'DECIMAL'"}, cause
    else:
        assert refusal["code"] == "recovery_refused", refusal
        assert refusal["details"].get("operation") == "logical_transfer", refusal
        assert refusal["details"].get("reason") == "unsupported_format", refusal
    assert result["changed_artifact"] is False
    assert result["changed_destination_parent"] is False


def type_fixture(shape):
    """Values are constructed only in current workers, never in the old importer."""
    from okto_grafx import DateValue, DecimalValue

    decimal = DecimalValue(12500, 12, 4)
    if shape == "decimal":
        return "amount DECIMAL(12,4)", {"amount": decimal}
    if shape == "typed_collections":
        return "xs LIST<MAP<ANY>>", {"xs": ({"num": 1}, {"num": "two", "nested": (False, None)})}
    assert shape in ("type_bundle", "labels_bundle")
    return ("amount DECIMAL(12,4),day DATE,xs LIST<STRUCT<num:DECIMAL(12,4),meta:MAP<ANY>>>",
            {"amount": decimal, "day": DateValue(2024, 2, 29),
             "xs": ({"num": decimal, "meta": {"list": ({"num": 1},), "none": None}},)})


def verify_types(db, shape):
    from okto_grafx.domain.model.schema import encode_tuple

    _, values = type_fixture(shape)
    projection = ",".join("n." + name for name in values)
    match = "MATCH(n)" if shape == "labels_bundle" else "MATCH(n:N)"
    assert db.execute(match + " RETURN " + projection + " ORDER BY n.id").rows == (tuple(values.values()),) * 2
    table = db.catalog.catalog.table("N")
    descriptors = [column.stored_type.describe() if column.stored_type else None for column in table.columns]
    assert descriptors == {"decimal": [None, None, None],
                           "typed_collections": [None, None, "LIST<MAP<ANY>>"],
                           "type_bundle": [None, None, None, None, "LIST<STRUCT<num:DECIMAL(12,4),meta:MAP<ANY>>>" ],
                           "labels_bundle": [None, None, None, None, "LIST<STRUCT<num:DECIMAL(12,4),meta:MAP<ANY>>>" ]}[shape]
    return {"descriptors": descriptors,
            "first_row": encode_tuple(table, (1, "one", *values.values())).hex()}


def worker(action, root, artifact, shape, profile, resume, batch_rows=1):
    import okto_grafx
    from okto_grafx import connect
    from okto_grafx.errors import GrafxError
    from okto_grafx.transfer import TransferLimits, export_graph, import_graph

    origin = Path(okto_grafx.__file__).resolve()
    assert origin.is_relative_to(Path(sys.prefix).resolve()), (origin, sys.prefix)
    installed = {p.relative_to(origin.parent).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(origin.parent.rglob("*.py"))}
    result = {"action": action, "origin": str(origin), "version": okto_grafx.__version__, "batch_rows": batch_rows,
              "python": sys.version, "installed_python_files": installed, "resume": resume}
    result["files"] = {p.relative_to(origin.parent).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in sorted(origin.parent.rglob("*")) if p.is_file()
                       and "__pycache__" not in p.parts and p.suffix not in (".pyc", ".pyo")}
    typed = shape in ("decimal", "typed_collections", "type_bundle", "labels_bundle")
    labeled = shape in ("node_labels", "labels_bundle")
    options = ({"codec": "pure", "vector_math": "pure", "checksum": "pure"} if profile == "pure" else
               {"codec": "numpy", "vector_math": "numpy", "checksum": "native"})
    relation = "N" if shape == "namespace" else "R"
    root.mkdir(parents=True, exist_ok=False)
    limits = TransferLimits(batch_rows=batch_rows)
    expected_rows = ((1, "one", "edge-a", 2), (1, "one", "edge-b", 2), (2, "two", "loop", 2))
    label_pattern = "" if labeled else ":N"
    query = f"MATCH(n{label_pattern})-[e:{relation}]->(m{label_pattern}) RETURN n.id,n.body,e.body,m.id ORDER BY n.id,e.body"
    if action == "seed":
        with connect(root / "source", **options) as db:
            if shape != "plain":
                db.ensure_identity_indexes()
            with db.begin("write") as tx:
                if shape != "flexible":
                    suffix = type_fixture(shape)[0] + "," if typed else ""
                    tx.execute(f"CREATE NODE TABLE N(id INT64,body STRING,{suffix}PRIMARY KEY(id))")
                    tx.execute(f"CREATE REL TABLE {relation}(FROM N TO N,body STRING)")
                tx.execute("CREATE(:N {id:1,body:'one'})")
                tx.execute("CREATE(:N {id:2,body:'two'})")
                if typed:
                    values = type_fixture(shape)[1]
                    tx.execute("MATCH(n:N) SET " + ",".join(f"n.{name}=${name}" for name in values), values)
                for left, right, body in ((1, 2, "edge-a"), (1, 2, "edge-b"), (2, 2, "loop")):
                    tx.execute(f"MATCH(a:N {{id:$left}}),(b:N {{id:$right}}) CREATE(a)-[:{relation} {{body:$body}}]->(b)",
                               {"left": left, "right": right, "body": body})
                if shape == "flexible":
                    tx.execute("MATCH(n:N {id:1}) SET n.mixed=$value", {"value": {"nested": [1, "two", None]}})
                    tx.execute("MATCH(n:N {id:2}) SET n.mixed=7")
                if labeled:
                    tx.execute("MATCH(n:N {id:1}) SET n:LabelA:`λ` REMOVE n:N")
                    tx.execute("MATCH(n:N {id:2}) REMOVE n:N")
            assert db.execute(query).rows == expected_rows
            if typed:
                result["types"] = verify_types(db, shape)
            if shape == "flexible":
                assert db.execute("MATCH(n:N) RETURN n.mixed ORDER BY n.id").rows == (({"nested": (1, "two", None)},), (7,))
            exported = export_graph(db, artifact, limits=limits)
            assert exported.rows == 5
        manifest = json.loads((artifact / "manifest.json").read_bytes())
        expected_format = "okto-grafx-logical-" + ("4" if labeled else "1" if typed else {"plain": "1", "flexible": "2", "namespace": "3"}[shape])
        assert manifest["format"] == expected_format
        result["format"] = expected_format
        result["manifest_sha256"] = hashlib.sha256((artifact / "manifest.json").read_bytes()).hexdigest()
        return result
    before_artifact, before_root = snapshot(artifact), snapshot(root)
    target = root / "target"
    kwargs = {"resume_directory": root / "workspace"} if resume else {}
    if action == "refuse":
        try:
            import_graph(artifact, target, limits=limits, **kwargs)
        except GrafxError as exc:
            result["refusal"] = {"code": exc.code, "details": dict(exc.details)}
            if typed:
                result["refusal"]["cause"] = {"type": type(exc.__cause__).__name__, "message": str(exc.__cause__)}
        except (KeyError, TypeError) as exc:
            if not typed or not resume:
                raise
            result["refusal"] = {"code": None, "details": {},
                                 "cause": {"type": type(exc).__name__, "message": str(exc)}}
        else:
            raise AssertionError("old importer accepted unsupported format")
        result["changed_artifact"] = snapshot(artifact) != before_artifact
        result["changed_destination_parent"] = snapshot(root) != before_root
        if typed:
            result.update(artifact_before=before_artifact, artifact_after=snapshot(artifact),
                          destination_before=before_root, destination_after=snapshot(root))
        validate_refusal(result, shape=shape)
        return result
    assert action == "import"
    imported = import_graph(artifact, target, limits=limits, **kwargs)
    assert imported.rows == 5 and len(imported.record_id_mapping) == 5
    assert imported.target_database_uuid != imported.source_database_uuid
    if resume:
        assert import_graph(artifact, target, limits=limits, **kwargs) == imported
    for _ in range(2):
        with connect(target, read_only=True, **options) as db:
            assert db.execute(query).rows == expected_rows
            if typed:
                result["types"] = verify_types(db, shape)
            if shape == "flexible":
                assert db.execute("MATCH(n:N) RETURN n.mixed ORDER BY n.id").rows == (({"nested": (1, "two", None)},), (7,))
            if shape == "namespace":
                assert {m.kind for m in imported.record_id_mapping} == {"node", "rel"}
                assert len({(m.kind, m.table, m.source_record_id) for m in imported.record_id_mapping}) == 5
            if labeled:
                rows = db.execute("MATCH(n) RETURN n.id,labels(n) ORDER BY n.id").rows
                assert rows == ((1, ("LabelA", "λ")), (2, ())), rows
                result["labels"] = list(rows)
            assert db.verify("all").findings == ()
    assert snapshot(artifact) == before_artifact
    result.update(verified=True, reopens=2, rows=imported.rows, changed_artifact=False)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("seed", "import", "refuse"))
    parser.add_argument("--path", type=Path)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--shape", choices=("plain", "flexible", "namespace", "decimal", "typed_collections", "type_bundle", "node_labels", "labels_bundle"), default="plain")
    parser.add_argument("--types-only", action="store_true", help="Qualify only native type descriptors with a preceding temporal-capable importer")
    parser.add_argument("--labels-only", action="store_true", help="Qualify format 4 alone and with the complete native type bundle")
    parser.add_argument("--profile", choices=("pure", "accelerated"), default="pure")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--batch-rows", type=int, default=1)
    parser.add_argument("--old-python", action="append", type=Path, default=[])
    parser.add_argument("--current-python", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.types_only and args.labels_only:
        parser.error("select only one focused qualification mode")
    if args.worker:
        print(json.dumps(worker(args.worker, args.path.resolve(), args.artifact.resolve(), args.shape, args.profile, args.resume, args.batch_rows)))
        return
    if not args.old_python or args.current_python is None or args.output is None:
        parser.error("supply old/current interpreters and a fresh output directory")
    old = [p.resolve(strict=True) for p in args.old_python]
    current = args.current_python.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    report = {"kind": "installed_wheel_logical_transfer", "cases": []}

    def run(name, interpreter, action, artifact, shape, profile, resume=False, batch_rows=1):
        argv = [str(interpreter), "-I", str(Path(__file__).resolve()), "--worker", action,
                "--path", str(output / name), "--artifact", str(artifact), "--shape", shape, "--profile", profile,
                "--batch-rows", str(batch_rows)]
        if resume:
            argv.append("--resume")
        process = subprocess.run(argv, text=True, capture_output=True, timeout=120)
        case = {"name": name, "interpreter": str(interpreter), "action": action,
                "shape": shape, "profile": profile, "resume": resume, "batch_rows": batch_rows, "exit_code": process.returncode,
                "stderr": process.stderr, "stdout": process.stdout,
                "status": "passed" if process.returncode == 0 else "failed"}
        report["cases"].append(case)
        (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(name, case["status"], flush=True)
        if process.returncode:
            raise SystemExit(1)

    if args.types_only or args.labels_only:
        for profile in ("pure", "accelerated"):
            for shape in (("node_labels", "labels_bundle") if args.labels_only else ("decimal", "typed_collections", "type_bundle")):
                artifact = output / f"current-{profile}-{shape}-artifact"
                run(f"current-{profile}-{shape}-seed", current, "seed", artifact, shape, profile)
                for resume in (False, True):
                    run(f"current-{profile}-{shape}-import-{resume}", current, "import", artifact, shape, profile, resume)
                    for ordinal, interpreter in enumerate(old):
                        run(f"old{ordinal}-{profile}-{shape}-refuse-{resume}", interpreter, "refuse",
                            artifact, shape, profile, resume)
        return
    for profile in ("pure", "accelerated"):
        artifacts = {}
        for shape in ("plain", "flexible", "namespace"):
            artifact = output / f"current-{profile}-{shape}-artifact"
            artifacts[shape] = artifact
            run(f"current-{profile}-{shape}-seed", current, "seed", artifact, shape, profile)
            if shape != "plain":
                for resume in (False, True):
                    run(f"current-{profile}-{shape}-import-{resume}", current, "import", artifact, shape, profile, resume)
        for ordinal, interpreter in enumerate(old):
            artifact = output / f"old{ordinal}-{profile}-artifact"
            run(f"old{ordinal}-{profile}-seed", interpreter, "seed", artifact, "plain", profile)
            run(f"old{ordinal}-{profile}-to-current", current, "import", artifact, "plain", profile)
            # Archived importers have a separately reproduced identity-floor bug
            # between tiny batches. Preserve their default 256-row contract here;
            # current imports above still exercise the one-row boundary.
            run(f"current-to-old{ordinal}-{profile}", interpreter, "import", artifacts["plain"], "plain", profile, batch_rows=256)
            for shape in ("flexible", "namespace"):
                for resume in (False, True):
                    run(f"old{ordinal}-{profile}-{shape}-refuse-{resume}", interpreter, "refuse",
                        artifacts[shape], shape, profile, resume)


if __name__ == "__main__":
    main()
