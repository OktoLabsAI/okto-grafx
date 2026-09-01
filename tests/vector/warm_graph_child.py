"""One writer process for the warm-graph cross-process test.

Run as a script by a fresh interpreter, so it assumes nothing about the parent beyond the paths
it inserts itself (A94). It drives the PUBLIC door only: ``connect``, ``begin("write")``,
``execute``, ``close``.
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
for _entry in (str(HERE), str(PROJECT_ROOT / "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from okto_grafx import connect  # noqa: E402


def main(root: str, record_id: int, *, with_vector: bool = True) -> int:
    report: dict[str, object] = {
        "record_id": record_id,
        "committed": False,
        "failure": None,
    }
    try:
        database = connect(root, vector_exact_scan_threshold=0)
        try:
            with database.begin("write") as txn:
                if with_vector:
                    a, b = 1.0 - record_id * 0.05, record_id * 0.05
                    txn.execute(
                        "CREATE (:V {id: $i, e: [$a, $b, 0.25, 0.0]})",
                        {"i": record_id, "a": a, "b": b},
                    )
                else:
                    txn.execute("CREATE (:V {id: $i, e: NULL})", {"i": record_id})
            report["committed"] = True
        finally:
            database.close()
    except Exception:  # noqa: BLE001 - the report is the whole point of the child
        report["failure"] = traceback.format_exc()
    print(json.dumps(report), flush=True)
    return 0 if report["committed"] else 1


if __name__ == "__main__":
    sys.exit(
        main(
            sys.argv[1],
            int(sys.argv[2]),
            with_vector=len(sys.argv) < 4 or sys.argv[3] != "sparse",
        )
    )
