"""Verify real installed 0.0.5 legacy and current 0.0.7 wheels, additive upgrades and old-reader refusal.

Both wheels are installed without dependencies into temporary isolated targets;
no source checkout, production database or global installation is used by workers.
Runtime dependencies must already be installed in the invoking test environment.
"""

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

_WORKER = r'''
import sys, json
from pathlib import Path
package, path, operation, profile = sys.argv[1:]
sys.path.insert(0, package)
import okto_grafx
assert Path(okto_grafx.__file__).resolve().is_relative_to(Path(package).resolve())
from okto_grafx import connect
from okto_grafx.errors import GrafxSchemaVersionMismatch
selectors = dict(checksum='pure', codec='pure', vector_math='pure') if profile == 'pure' else dict(checksum='native', codec='numpy', vector_math='numpy')
try:
    with connect(path, read_only=operation == 'read', **selectors) as db:
        if operation == 'seed':
            with db.begin() as tx:
                tx.execute('CREATE NODE TABLE D(id INT64,v STRING,PRIMARY KEY(id))')
                tx.execute("CREATE (:D {id:1,v:'legacy graph'})")
        elif operation == 'append':
            with db.begin() as tx:
                tx.execute("CREATE (:D {id:2,v:'current graph'})")
        elif operation == 'posting':
            db.create_index('v', 'D', ('v',), layout='posting_hash', bucket_count=4)
        elif operation == 'nullable':
            db.ensure_identity_indexes()
            from okto_grafx.domain.model.schema import ColumnDef
            from okto_grafx.domain.model.value import ValueType
            db.add_nullable_column('D', ColumnDef('extra', ValueType.STRING))
        elif operation in ('system_history', 'history_index', 'history_compaction'):
            db.ensure_identity_indexes()
            db.enable_commit_history()
            db.enable_system_history(('D',))
            at = db.commit_history().entries[-1].identity
            assert len(db.system_as_of(at, tables=('D',)).rows) == 2
            db.pin_system_history('matrix', at, tables=('D',))
            assert len(db.system_history_pins()) == 1
            if operation != 'system_history':
                db.enable_system_history_index()
                assert len(db.system_versions('D', db.system_as_of(at, tables=('D',)).rows[0].record_id).versions) == 1
            if operation == 'history_compaction':
                db.unpin_system_history('matrix')
                for i in range(4):
                    with db.begin() as tx:
                        tx.execute('MATCH (d:D {id:1}) SET d.v=$v', {'v':str(i)*8000})
                with db.begin() as tx:
                    tx.execute("MATCH (d:D {id:1}) SET d.v='retained'")
                at = db.commit_history().entries[-1].identity
                db.prune_system_history(at, tables=('D',))
                assert db.compact_system_history(confirm_quiescent=True).physical_bytes_reclaimed > 0
        elif operation == 'positions':
            from okto_grafx import TextIndexOptions
            db.create_text_index('fts', 'D', ('v',), bucket_count=4,
                options=TextIndexOptions(positions=True, statistics_mode='durable'))
            assert len(db.search_text(index='fts', query='legacy graph', phrase=True).hits) == 1
        rows = db.execute('MATCH (d:D) RETURN d.id ORDER BY d.id').rows
        assert db.verify().clean
        if operation != 'read': db.checkpoint()
        print(json.dumps(dict(version=okto_grafx.__version__, outcome='opened', rows=rows)))
except GrafxSchemaVersionMismatch:
    print(json.dumps(dict(version=okto_grafx.__version__, outcome='required_capability_refused')))
'''


def run(package, path, operation, profile):
    """Qualified isolated interpreter; a traceback is a failed matrix cell, never a skip."""
    result = subprocess.run([sys.executable, "-I", "-c", _WORKER, str(package), str(path), operation, profile],
                            capture_output=True, text=True, timeout=90)
    if result.returncode:
        raise RuntimeError(f"{operation}/{profile} failed: {result.stderr}")
    return json.loads(result.stdout)


def payloads(path):
    """Observe payload integrity across old-reader refusal, excluding coordination artifacts."""
    return {str(item.relative_to(path)): hashlib.sha256(item.read_bytes()).hexdigest()
            for item in path.rglob("*") if item.is_file() and (
                item.suffix in (".dat", ".meta", ".dir") or item.parent.name == "index")}


def main():
    """Run upgrade/readback cells against frozen artifact hashes, never source imports."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-wheel", type=Path, required=True)
    parser.add_argument("--current-wheel", type=Path, required=True)
    args = parser.parse_args()
    wheels = [path.resolve(strict=True) for path in (args.legacy_wheel, args.current_wheel)]
    if any(path.suffix != ".whl" for path in wheels):
        parser.error("Both inputs must be wheel files")
    evidence = []
    with tempfile.TemporaryDirectory(prefix="grafx-v006-matrix-") as directory:
        root = Path(directory)
        packages = [root / "old-package", root / "new-package"]
        for wheel, package in zip(wheels, packages):
            subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(package), str(wheel)],
                           check=True, capture_output=True, text=True, timeout=120)
        for profile in ("pure", "accelerated"):
            for capability in ("default", "posting", "nullable", "system_history", "positions", "history_index", "history_compaction"):
                path = root / (profile + "-" + capability)
                seed = run(packages[0], path, "seed", profile)
                assert seed == dict(version="0.0.5", outcome="opened", rows=[[1]]), seed
                upgraded = run(packages[1], path, "append", profile)
                assert upgraded == dict(version="0.0.7", outcome="opened", rows=[[1], [2]]), upgraded
                if capability != "default":
                    assert run(packages[1], path, capability, profile)["outcome"] == "opened"
                before = payloads(path)
                old = run(packages[0], path, "read", profile)
                expected = "opened" if capability == "default" else "required_capability_refused"
                assert old["outcome"] == expected and payloads(path) == before, old
                opposite = "accelerated" if profile == "pure" else "pure"
                reopened = run(packages[1], path, "read", opposite)
                assert reopened["outcome"] == "opened" and reopened["rows"] == [[1], [2]], reopened
                evidence.append(dict(case=capability, writer=profile, readback=opposite,
                                     upgrade=upgraded, old_reader=old, payloads_unchanged=True))
    print(json.dumps(dict(platform=platform.platform(), python=platform.python_version(),
        artifacts=[dict(file=p.name, sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in wheels],
        cells=evidence), indent=2))


if __name__ == "__main__":
    main()
