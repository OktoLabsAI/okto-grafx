"""Exercise a real 0.0.4 wheel against current source in isolated subprocesses.

Usage: python tools/check_v005_upgrade.py --legacy-wheel path/to/okto_grafx-0.0.4.whl
Creates only a new temporary directory, prints JSON evidence, never opens Pulse data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def run(package: Path, path: Path, operation: str) -> dict:
    """Use -I plus an explicit package path so PYTHONPATH cannot spoof either version."""
    code = '''
import sys, json
sys.path.insert(0, sys.argv[1])
import okto_grafx
from okto_grafx import connect
from okto_grafx.errors import GrafxSchemaVersionMismatch
path, operation = sys.argv[2:]
try:
    with connect(path, checksum='pure', codec='pure', vector_math='pure') as db:
        if operation == 'seed':
            with db.begin() as tx:
                tx.execute('CREATE NODE TABLE D(id INT64,v STRING,PRIMARY KEY(id))')
                tx.execute("CREATE (:D {id:1,v:'legacy'})")
        elif operation == 'append':
            with db.begin() as tx:
                tx.execute("CREATE (:D {id:2,v:'current'})")
        elif operation == 'sparse':
            db.create_index('v', 'D', ('v',), layout='sparse_hash', bucket_count=128)
        elif operation == 'history':
            from okto_grafx import TextIndexOptions
            db.create_text_index('fts', 'D', ('v',), options=TextIndexOptions(statistics_mode='durable', statistics_history_entries=2))
        elif operation == 'free':
            db.ensure_identity_indexes()
            db.maintenance.vacuum(confirm_quiescent=True, index_free_pages=True)
        rows = db.execute('MATCH (d:D) RETURN d.id ORDER BY d.id').rows
        assert not db.verify('all').findings
        print(json.dumps({'version':okto_grafx.__version__, 'outcome':'opened', 'rows':rows}))
except GrafxSchemaVersionMismatch:
    print(json.dumps({'version':okto_grafx.__version__, 'outcome':'required_capability_refused'}))
'''
    process = subprocess.run([sys.executable, "-I", "-c", code, str(package), str(path), operation],
                             capture_output=True, text=True, timeout=90)
    if process.returncode:
        raise RuntimeError(f"{operation} failed under {package}: {process.stderr}")
    return json.loads(process.stdout)


def main() -> None:
    """Check upgrade and old-reader fencing; report exact artifact and platform identity."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-wheel", type=Path, required=True)
    args = parser.parse_args()
    wheel = args.legacy_wheel.resolve(strict=True)
    if not wheel.name.endswith(".whl"):
        parser.error("--legacy-wheel must name an existing wheel")
    evidence = []
    with tempfile.TemporaryDirectory(prefix="grafx-v005-upgrade-") as folder:
        for capability in ("default", "sparse", "history", "free"):
            path = Path(folder) / capability
            seed = run(wheel, path, "seed")
            assert seed["version"] == "0.0.4" and seed["rows"] == [[1]], seed
            current = run(ROOT / "src", path, "append")
            assert current["version"] == "0.0.5" and current["rows"] == [[1], [2]], current
            if capability != "default":
                activated = run(ROOT / "src", path, capability)
                assert activated["outcome"] == "opened", activated
            old = run(wheel, path, "read")
            expected = "opened" if capability == "default" else "required_capability_refused"
            assert old["outcome"] == expected, old
            evidence.append({"case": capability, "new_reads_old": current, "old_reads_new": old})
    print(json.dumps({"platform": platform.platform(), "python": platform.python_version(),
                      "legacy_wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                      "cases": evidence}, indent=2))


if __name__ == "__main__":
    main()
