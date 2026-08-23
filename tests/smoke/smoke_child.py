"""One writer process for the concurrent-append smoke test.

Run as a script by a fresh interpreter, so it assumes nothing about the parent beyond the paths
it inserts itself. It drives the PUBLIC door only -- ``connect``, ``begin("write")``, ``execute``
-- because the defect this exists for was invisible to every test that drove the engine directly.
"""

from __future__ import annotations

import json
import random
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

RETRY_BUDGET: int = 60
"""Attempts one transaction gets before the child gives up and says so.

Contention here is real and expected -- every append to one table touches that table's last page,
and BR-6 refuses a commit whose partitions intersect another's. The budget is far above what the
workload needs, so exhausting it is a finding rather than a tuning problem.
"""


def main(root: str, slot: int, rounds: int, per_round: int) -> int:
    rnd = random.Random(slot * 7919)
    database = connect(root)
    acknowledged: list[int] = []
    conflicts = 0
    failure: str | None = None
    try:
        for index in range(rounds):
            base = slot * 1_000_000 + index * per_round
            for _attempt in range(RETRY_BUDGET):
                try:
                    with database.begin("write") as txn:
                        for offset in range(per_round):
                            txn.execute(
                                "CREATE (:Item {id: $i, owner: $o, body: $b})",
                                {"i": base + offset + 1, "o": slot, "b": "y" * 120},
                            )
                    acknowledged.extend(base + offset + 1 for offset in range(per_round))
                    break
                except GrafxError as refused:
                    if not getattr(refused, "retryable", False):
                        raise
                    conflicts += 1
                    time.sleep(rnd.uniform(0.002, 0.02))
            else:
                raise AssertionError(f"round {index} exhausted {RETRY_BUDGET} attempts")
    except BaseException:  # reported, never swallowed: the parent decides what it means
        failure = traceback.format_exc()[-1200:]
    try:
        database.close()
    except BaseException:
        # Reported, never swallowed. close() is where the pool settles the pages an abandoned
        # attempt allocated, so a child that hid a failure here would be hiding one from the
        # very door this test is the evidence for.
        closing = traceback.format_exc()[-600:]
        failure = f"{failure}\n--- and close() failed ---\n{closing}" if failure else closing
    print(
        json.dumps(
            {
                "slot": slot,
                "acknowledged": acknowledged,
                "conflicts": conflicts,
                "failure": failure,
            }
        ),
        flush=True,
    )
    return 1 if failure else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4])))
