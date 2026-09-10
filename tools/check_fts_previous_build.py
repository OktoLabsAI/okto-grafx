"""Check an archived pre-FTS src tree refuses activation without authoritative writes.

Usage: python tools/check_fts_previous_build.py PATH_TO_PREVIOUS_SRC
Run with current src on PYTHONPATH. Uses only fresh temporary stores.
"""

from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
from tempfile import TemporaryDirectory

from okto_grafx import connect


def main() -> None:
    """Exercise checkpointed and durable-WAL-only activation in both open modes."""
    baseline = str(Path(sys.argv[1]).resolve(strict=True))
    current = str(Path(__file__).resolve().parents[1] / "src")
    receipts = []
    for cut in ("checkpoint", "wal"):
        with TemporaryDirectory() as temp:
            path = Path(temp) / "db"
            with connect(path) as db:
                with db.begin() as tx:
                    tx.execute(
                        "CREATE NODE TABLE Doc(id INT64, body STRING, PRIMARY KEY(id))"
                    )
                    tx.execute("CREATE (:Doc {id:1, body:'wal proof'})")
                db.checkpoint()
            activation = """import os, sys
from okto_grafx import connect
from okto_grafx.adapters.storage_local import LocalStorageDevice
db = connect(sys.argv[1])
original = LocalStorageDevice.durable_barrier
def cut(self, file):
    result = original(self, file)
    if file.startswith('wal/'):
        os._exit(73)
    return result
if sys.argv[2] == 'wal':
    LocalStorageDevice.durable_barrier = cut
db.create_text_index('text', 'Doc', ('body',))
db.checkpoint()
db.close()
"""
            child = subprocess.run(
                [sys.executable, "-c", activation, str(path), cut],
                env={**os.environ, "PYTHONPATH": current},
                cwd=temp,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert child.returncode == (73 if cut == "wal" else 0), child.stderr

            def digest() -> dict[str, str]:
                """Only runtime lock/reader registry files are outside the comparison."""
                return {
                    str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in path.rglob("*")
                    if p.is_file()
                    and (
                        p.suffix == ".dat"
                        or p.name in ("grafx.meta", "commit.state")
                        or "wal" in p.parts
                        or "index" in p.parts
                    )
                }

            before = digest()
            assert (
                "heap.dat" in before
                and "catalog.dat" in before
                and "grafx.meta" in before
            )
            probe = """import sys
from okto_grafx import connect
from okto_grafx.errors import GrafxError
try:
    with connect(sys.argv[1], read_only=sys.argv[2]=='1'): pass
except GrafxError as exc:
    print(type(exc).__name__, str(exc))
    sys.exit(0)
sys.exit(9)
"""
            for mode in ("1", "0"):
                child = subprocess.run(
                    [sys.executable, "-c", probe, str(path), mode],
                    env={**os.environ, "PYTHONPATH": baseline},
                    cwd=temp,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert child.returncode == 0, child.stdout + child.stderr
                assert digest() == before, "Old build changed authoritative files"
                receipts.append(
                    {
                        "activation": cut,
                        "read_only": mode == "1",
                        "refusal": child.stdout.strip(),
                        "unchanged_files": list(before),
                    }
                )
            with connect(path) as db:
                assert len(db.search_text(index="text", query="proof").hits) == 1
                assert not db.verify("all").findings
    print(json.dumps(receipts, indent=2))


if __name__ == "__main__":
    main()
