"""Small isolated public CAP-1 publication cost sample; no live Pulse data."""

import hashlib
import json
from pathlib import Path
import statistics
import tempfile
import time

from okto_grafx import connect


def main():
    samples = {False: [], True: []}
    with tempfile.TemporaryDirectory(prefix="grafx-journal-cost-") as raw:
        databases = {}
        try:
            for active in (False, True):
                db = connect(Path(raw) / str(active), page_size=512)
                databases[active] = db
                with db.begin("write") as tx:
                    tx.execute("CREATE NODE TABLE P(id INT64, name STRING, PRIMARY KEY(id))")
                    tx.execute("CREATE (:P {id:1,name:'seed'})")
                db.ensure_identity_indexes()
                if active:
                    db.enable_commit_history()
            for iteration in range(18):
                for active in ((False, True) if iteration % 2 else (True, False)):
                    db = databases[active]
                    start = time.perf_counter()
                    with db.begin("write") as tx:
                        tx.execute("MATCH (p:P {id:1}) SET p.name=$v", {"v": f"v{iteration}"})
                    elapsed = 1000 * (time.perf_counter() - start)
                    if iteration >= 2:
                        samples[active].append(elapsed)
            for db in databases.values():
                assert db.execute("MATCH (p:P) RETURN p.name").rows == (("v17",),)
                db.checkpoint()
            db = databases[True]
            assert len(db.commit_history().entries) == 18
            assert db.verify().clean
        finally:
            for db in databases.values():
                db.close()
    source = Path(__file__).resolve().parents[2] / "src/okto_grafx/engine/txn_manager.py"
    print(json.dumps({"scope": "single-node update, 512-byte pages, 16 samples per mode, alternating order",
                      "txn_manager_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                      "samples_ms": samples,
                      "median_ms": {str(active): statistics.median(values) for active, values in samples.items()}}, indent=2))


if __name__ == "__main__":
    main()
