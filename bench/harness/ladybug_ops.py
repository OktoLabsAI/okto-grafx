"""The same three operations on LadybugDB 0.16, the D5 reference engine.

Run in a subprocess, always
---------------------------
``calibrate.py`` never imports this module in its own process. Every baseline reading is taken by
spawning ``python -m bench.harness.ladybug_ops``, which prints one JSON document and exits. Three
reasons, in order of importance:

1. The reference engine is a native extension. A segmentation fault in it would take the harness
   with it and leave the calibration with no artefact at all; in a child it is an exit status,
   and the ceiling it belongs to is reported UNMEASURED (A75.2) while the others still stand.
2. A child can be bounded by a timeout, so a hang becomes a red result rather than a stalled CI
   job (A42/A88).
3. The crashed-database state that ``open_replay`` needs can only be produced by a process that
   dies without closing its database, which is what ``--operation crash`` does.

Fairness, stated rather than assumed
------------------------------------
* ``durable_commit`` is one auto-commit ``CREATE`` statement: one durable transaction of one small
  node, which is what the Okto Grafx side commits too.
* ``point_read`` is a primary-key ``MATCH`` returning one property. The reference pays a parse and
  a plan that the Okto Grafx heap read does not, because Okto Grafx has no query surface yet. The
  bias favours Okto Grafx and is recorded in the artefact.
* ``open_replay`` opens a database whose writer was killed with an unflushed log, so the open
  really does replay. The fixture is proved: after the timed opens, the last database is queried
  for the row count that was written, and a replay that restored nothing is an error rather than
  a fast result (A72).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

__all__ = ["build_parser", "main"]

TABLE_DDL: str = "CREATE NODE TABLE Bench(id INT64, payload STRING, PRIMARY KEY(id))"
"""The one table every baseline operation uses."""

PAYLOAD: str = "payload-" + "x" * 24
"""A small, fixed row payload, so the measurement is about the engine and not about the bytes."""


def _database(path: str) -> tuple[object, object]:
    """Open a LadybugDB database and a connection over it, returning both."""
    import ladybug

    database = ladybug.Database(path)
    return database, ladybug.Connection(database)


def _report(samples: list[float], warmup: list[float], detail: str) -> None:
    """Print the JSON document the parent reads, then leave without a teardown."""
    sys.stdout.write(
        json.dumps({"samples": samples, "warmup": warmup, "detail": detail}) + "\n"
    )
    sys.stdout.flush()
    # Teardown is not part of any measurement here, and a native checkpoint on close has been
    # observed to abort this engine's process. Leaving without it keeps a teardown crash from
    # turning a completed measurement into an unreadable one.
    os._exit(0)


def _durable_commit(root: Path, iterations: int, warmup: int) -> None:
    """Time one auto-commit CREATE per iteration."""
    _database_path = str(root / "commit.db")
    _, connection = _database(_database_path)
    connection.execute(TABLE_DDL)
    samples: list[float] = []
    warmup_samples: list[float] = []
    for index in range(warmup + iterations):
        start = time.perf_counter()
        connection.execute(
            "CREATE (:Bench {id: $id, payload: $payload})",
            {"id": index, "payload": PAYLOAD},
        )
        elapsed = time.perf_counter() - start
        (warmup_samples if index < warmup else samples).append(elapsed)
    _report(samples, warmup_samples, "ladybug auto-commit CREATE of one node")


def _point_read(root: Path, iterations: int, warmup: int, rows: int) -> None:
    """Time one primary-key MATCH per iteration over a warm database."""
    _, connection = _database(str(root / "read.db"))
    connection.execute(TABLE_DDL)
    for index in range(rows):
        connection.execute(
            "CREATE (:Bench {id: $id, payload: $payload})",
            {"id": index, "payload": PAYLOAD},
        )
    samples: list[float] = []
    warmup_samples: list[float] = []
    seen = 0
    for index in range(warmup + iterations):
        key = index % rows
        start = time.perf_counter()
        result = connection.execute(
            "MATCH (n:Bench {id: $id}) RETURN n.payload", {"id": key}
        )
        row = result.get_next() if result.has_next() else None
        elapsed = time.perf_counter() - start
        if row is None:
            raise SystemExit(f"the baseline point read of row {key} returned nothing")
        seen += 1
        (warmup_samples if index < warmup else samples).append(elapsed)
    if seen != warmup + iterations:
        raise SystemExit("the baseline point read did not run every iteration")
    _report(samples, warmup_samples, f"ladybug primary-key MATCH over {rows} rows")


def _crash(root: Path, records: int) -> None:
    """Write ``records`` rows and die without closing, leaving a log to replay."""
    path = str(root / "crashed.db")
    _, connection = _database(path)
    connection.execute(TABLE_DDL)
    for index in range(records):
        connection.execute(
            "CREATE (:Bench {id: $id, payload: $payload})",
            {"id": index, "payload": PAYLOAD},
        )
    sys.stdout.write(json.dumps({"crashed": path, "records": records}) + "\n")
    sys.stdout.flush()
    # No close, no checkpoint: exactly the state an open must recover from.
    os._exit(0)


def _copy_crashed(source: Path, target: Path) -> None:
    """Copy a crashed database and every sidecar the engine left beside it."""
    for candidate in sorted(source.parent.glob(source.name + "*")):
        destination = target.parent / (target.name + candidate.name[len(source.name) :])
        if candidate.is_dir():
            shutil.copytree(candidate, destination)
        else:
            shutil.copy2(candidate, destination)


def _open_replay(root: Path, iterations: int, warmup: int, crashed: str, records: int) -> None:
    """Time opening a copy of a crashed database, which replays its log."""
    import ladybug

    source = Path(crashed)
    samples: list[float] = []
    warmup_samples: list[float] = []
    last: object | None = None
    for index in range(warmup + iterations):
        target = root / f"replay-{index}.db"
        _copy_crashed(source, target)
        start = time.perf_counter()
        database = ladybug.Database(str(target))
        elapsed = time.perf_counter() - start
        (warmup_samples if index < warmup else samples).append(elapsed)
        last = database
    # A72: prove the fixture produced the state it claims. A replay that restored nothing would
    # be the fastest reading in this file and the most useless one.
    connection = ladybug.Connection(last)
    result = connection.execute("MATCH (n:Bench) RETURN count(n)")
    restored = result.get_next()[0] if result.has_next() else 0
    if restored != records:
        raise SystemExit(
            f"the replayed database holds {restored} rows and the crashed writer wrote {records}"
        )
    _report(
        samples,
        warmup_samples,
        f"ladybug open of a copy of a crashed database that replays {records} rows",
    )


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser of the baseline worker."""
    parser = argparse.ArgumentParser(
        prog="python -m bench.harness.ladybug_ops",
        description="Take one LadybugDB 0.16 baseline measurement and print it as JSON.",
    )
    parser.add_argument(
        "--operation",
        required=True,
        choices=("durable_commit", "point_read", "crash", "open_replay"),
        help="which baseline to measure",
    )
    parser.add_argument("--root", required=True, help="a directory this worker may fill")
    parser.add_argument("--iterations", type=int, default=30, help="samples to keep")
    parser.add_argument("--warmup", type=int, default=5, help="samples to discard")
    parser.add_argument("--rows", type=int, default=512, help="corpus size for the read")
    parser.add_argument("--records", type=int, default=2000, help="rows the crashed writer wrote")
    parser.add_argument("--crashed", default="", help="path of the crashed database to replay")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run one baseline operation. Every failure is an exit status the parent can read."""
    arguments = build_parser().parse_args(argv)
    root = Path(arguments.root)
    root.mkdir(parents=True, exist_ok=True)
    if arguments.operation == "durable_commit":
        _durable_commit(root, arguments.iterations, arguments.warmup)
    elif arguments.operation == "point_read":
        _point_read(root, arguments.iterations, arguments.warmup, arguments.rows)
    elif arguments.operation == "crash":
        _crash(root, arguments.records)
    else:
        if not arguments.crashed:
            raise SystemExit("--operation open_replay needs --crashed")
        _open_replay(
            root, arguments.iterations, arguments.warmup, arguments.crashed, arguments.records
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
