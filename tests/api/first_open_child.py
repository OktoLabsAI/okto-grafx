"""One participant of the concurrent first-open test: open a fresh database and report its identity.

Run as a script by a fresh interpreter, so it assumes nothing about the parent beyond the paths
it inserts itself (A94). It drives the PUBLIC door only. Every child waits for the parent's
``go`` marker so that the opens really overlap, and reports what happened as one JSON line:
the identity it was handed, or the typed refusal it met, or the traceback of anything else.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
for _entry in (str(HERE), str(PROJECT_ROOT / "src")):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)

from okto_grafx import connect  # noqa: E402
from okto_grafx.domain.errors import GrafxError  # noqa: E402


def main(root: str, slot: int, ready_marker: str, go_marker: str, budget: float) -> int:
    report: dict[str, object] = {
        "slot": slot,
        "outcome": None,
        "uuid": None,
        "detail": None,
    }
    Path(ready_marker).write_text("ready", encoding="utf-8")
    deadline = time.monotonic() + budget
    while not os.path.exists(go_marker):
        if time.monotonic() > deadline:
            report["outcome"] = "never_released"
            print(json.dumps(report), flush=True)
            return 2
        time.sleep(0.002)
    try:
        database = connect(root)
        try:
            report["outcome"] = "opened"
            report["uuid"] = str(database.identity.database_uuid)
            with database.begin("write") as txn:
                txn.execute(
                    f"CREATE NODE TABLE Marker{slot}(id INT64, PRIMARY KEY(id))"
                )
            report["detail"] = "wrote its own table"
        finally:
            database.close()
    except GrafxError as refused:
        report["outcome"] = "refused"
        report["detail"] = f"{type(refused).__name__}:{refused.code}"
    except Exception:  # noqa: BLE001 - the report is the whole point of the child
        report["outcome"] = "died"
        report["detail"] = traceback.format_exc()
    print(json.dumps(report), flush=True)
    return 0 if report["outcome"] in ("opened", "refused") else 1


if __name__ == "__main__":
    sys.exit(
        main(
            sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4], float(sys.argv[5])
        )
    )
