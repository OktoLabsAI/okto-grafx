"""Reproduce old-wheel missing vector artifacts and qualify explicit source repair.

No installation, deletion or existing output directory is accepted. Old writers
use isolated interpreters; current workers explicitly load the supplied source.
This is source-against-old-wheel evidence, not an installed-current-wheel claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


def data_tree(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()
            and p.relative_to(root).parts[0] != "control"}


def worker(action, path, profile, source):
    if action != "seed":
        sys.path.insert(0, str(source))
    import okto_grafx
    from okto_grafx import connect, VectorValue
    from okto_grafx.errors import GrafxIndexError

    origin = Path(okto_grafx.__file__).resolve()
    expected = Path(sys.prefix).resolve() if action == "seed" else source
    assert origin.is_relative_to(expected), (origin, expected)
    result = {"action": action, "origin": str(origin), "version": okto_grafx.__version__,
              "mode": "installed_old" if action == "seed" else "explicit_current_source"}
    options = ({"codec": "pure", "vector_math": "pure", "checksum": "pure"} if profile == "pure" else
               {"codec": "numpy", "vector_math": "numpy", "checksum": "native"})
    if action == "seed":
        with connect(path, **options) as db:
            with db.begin("write") as tx:
                tx.execute("CREATE VECTOR SPACE s {dimension:2,metric:'cosine'}")
                tx.execute("CREATE NODE TABLE N(id INT64,v VECTOR(s),PRIMARY KEY(id))")
                tx.execute("CREATE REL TABLE E(FROM N TO N,v VECTOR(s))")
            with db.begin("write") as tx:
                tx.execute("CREATE(:N {id:1,v:$v})", {"v": VectorValue((1.0, 0.0), 1, "float32")})
                tx.execute("MATCH(n:N) CREATE(n)-[:E {v:$v}]->(n)",
                           {"v": VectorValue((0.0, 1.0), 1, "float32")})
            db.checkpoint()
            assert db.execute("MATCH(:N)-[e:E]->(:N) RETURN count(e)").rows == ((1,),)
            assert tuple(i.name for i in db.vectors.indexes()) == ("vector_N_s",)
        assert not (path / "index/vector_E_s.idx").exists()
        result["missing_artifact"] = "index/vector_E_s.idx"
    elif action == "preflight":
        before = data_tree(path)
        assert "index/vector_E_s.idx" not in before
        with connect(path, read_only=True, **options) as db:
            assert db.execute("MATCH(:N)-[e:E]->(:N) RETURN count(e)").rows == ((1,),)
            with db.begin("read") as tx:
                try:
                    db.search_vectors(tx, table=("rel", "E"), space="s", query=[1.0, 0.0], k=1)
                except GrafxIndexError:
                    pass
                else:
                    raise AssertionError("missing vector owner must refuse search")
        assert data_tree(path) == before
        result["read_only_changed_data_files"] = []
    elif action.startswith("cut_"):
        from okto_grafx.engine.database import Transaction
        from okto_grafx.engine.txn_manager import TransactionManager

        with connect(path, **options) as db:
            active = None
            original_commit, original_apply = Transaction.commit, TransactionManager._apply_images

            def stop_commit(tx):
                nonlocal active
                active = tx
                if action == "cut_before_commit":
                    os._exit(71)
                return original_commit(tx)

            def stop_apply(manager, images):
                if active is not None and active._context.state.value == "committed":
                    os._exit(73)
                return original_apply(manager, images)

            Transaction.commit, TransactionManager._apply_images = stop_commit, stop_apply
            db.rebuild_vector_index("s", table=("rel", "E"))
        raise AssertionError("requested process cut was not reached")
    elif action == "repair":
        with connect(path, **options) as db:
            table_id = db.catalog.catalog.table("E", kind="rel").table_id
            assert db.vectors.index("s", table_id=table_id).stale
            sibling = db.vectors.index("s", table_id=db.catalog.catalog.table("N").table_id)
            assert sibling.name == "vector_N_s" and not sibling.stale
            repaired = db.rebuild_vector_index("s", table=("rel", "E"))
            assert repaired.name == "vector_E_s" and repaired.table_id == table_id and not repaired.stale
            assert db.verify("all").findings == ()
            db.checkpoint()
        before = data_tree(path)
        for _ in range(2):
            with connect(path, read_only=True, **options) as db:
                assert db.execute("MATCH(:N)-[e:E]->(:N) RETURN count(e)").rows == ((1,),)
                with db.begin("read") as tx:
                    for table, score in ((("node", "N"), 1.0), (("rel", "E"), 0.0)):
                        hits = db.search_vectors(tx, table=table, space="s", query=[1.0, 0.0], k=1).hits
                        assert len(hits) == 1 and abs(hits[0].score - score) < 1e-6
                assert db.verify("all").findings == ()
        assert data_tree(path) == before
        result["reopens"] = 2
        result["verified"] = True
    else:
        raise AssertionError(action)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", choices=("seed", "preflight", "cut_before_commit", "cut_before_apply", "repair"))
    parser.add_argument("--path", type=Path)
    parser.add_argument("--profile", choices=("pure", "accelerated"), default="pure")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--old-python", type=Path, action="append", default=[])
    parser.add_argument("--current-python", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    if args.worker:
        print(json.dumps(worker(args.worker, args.path.resolve(), args.profile, source)))
        return
    if not args.old_python or args.current_python is None or args.output is None:
        parser.error("supply old/current interpreters and a fresh output directory")
    interpreters = [p.resolve(strict=True) for p in args.old_python]
    current = args.current_python.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source_files = {p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((source / "okto_grafx").rglob("*.py"))}
    report = {"kind": "old_installed_to_current_source_vector_repair",
              "source_files": source_files, "cases": []}
    script = Path(__file__).resolve()
    for ordinal, old in enumerate(interpreters):
        for profile in ("pure", "accelerated"):
            for cut in (None, "before_commit", "before_apply"):
                name = f"old{ordinal}-{profile}-{cut or 'ordinary'}"
                path = output / name / "db"
                steps = [(old, "seed", 0), (current, "preflight", 0)]
                if cut:
                    steps.append((current, "cut_" + cut, 71 if cut == "before_commit" else 73))
                steps.append((current, "repair", 0))
                case = {"name": name, "old_python": str(old), "profile": profile, "cut": cut, "steps": []}
                for interpreter, action, expected in steps:
                    process = subprocess.run([str(interpreter), "-I", str(script), "--worker", action,
                        "--path", str(path), "--source", str(source), "--profile", profile],
                        capture_output=True, text=True, timeout=120)
                    step = {"action": action, "exit_code": process.returncode,
                            "expected_exit_code": expected, "stdout": process.stdout, "stderr": process.stderr}
                    case["steps"].append(step)
                    if process.returncode != expected:
                        case["status"] = "failed"
                        break
                else:
                    case["status"] = "passed"
                report["cases"].append(case)
                (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
                print(name, case["status"], flush=True)
    if any(case["status"] != "passed" for case in report["cases"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
