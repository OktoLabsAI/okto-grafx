"""Shared scaffolding of the M0B black-box probes: a schema, rows, and the two crash shapes.

M0B (the recovery foundation, Codex) closes P0.1, P0.4 and P1.3. The probes in the
``test_m0b_*`` modules state the invariants it must establish as tests a reviewer can run
without reading the implementation, through the public door, with the fault bench of FR-16 over
the memory device (``power_loss_support``) or byte surgery on a real directory. Each probe that
fails on the base it was written on (8c88e9d + the M0B-prep probes) is a strict xfail carrying
the observed outcome; M0B flips those to failures and removes the markers.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from okto_grafx import connect
from okto_grafx.adapters.storage_fault import SimulatedCrash
from okto_grafx.engine.catalog_store import CATALOG_FILE
from okto_grafx.runtime.bootstrap import release_ports

from power_loss_support import bench_registry, durable_names, file_bytes, power_loss


def schema(database: Any) -> None:
    """Declare the one table every probe uses, with a primary key so an index exists."""
    with database.begin("write") as txn:
        txn.execute("CREATE NODE TABLE Person(id INT64, name STRING, PRIMARY KEY(id))")


def insert(database: Any, record_id: int) -> None:
    """Commit one row."""
    with database.begin("write") as txn:
        txn.execute(
            "CREATE (:Person {id: $i, name: $n})",
            {"i": record_id, "n": f"p{record_id}"},
        )


def ids(database: Any) -> tuple[Any, ...]:
    """Return the ids the table answers, through an autocommit read."""
    return tuple(row[0] for row in database.execute("MATCH (p:Person) RETURN p.id"))


def data_tree(root: Path) -> dict[str, str]:
    """Return every DATA file under a real directory with the digest of its bytes (control/ excluded)."""
    tree: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root)).replace("\\", "/")
        if path.is_file() and not relative.startswith("control/"):
            tree[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return tree


def crash_before_the_commit_state_publication(seed: int) -> tuple[Any, Any, Any]:
    """Return a composition whose log holds a durable COMMIT above ``commit.state``.

    The crash is placed by survey: one identical composition runs the commit undisturbed and
    the bench reports its write points; the ``atomic_replace`` that publishes ``commit.state``
    is the point, and a fresh composition crashes right before it (process death: everything
    already written stays, nothing after it happens). The schema is checkpointed first, so the
    published position is the checkpoint's and the crashed commit is the only thing above it.
    """
    registry, bench, inner = bench_registry(seed)
    database = connect(":memory:", registry=registry)
    schema(database)
    database.checkpoint()

    def one_commit(device: Any) -> None:
        insert(database, 1)

    points = bench.enumerate_write_points(one_commit)
    publication = [
        point.call_index
        for point in points
        if point.method == "atomic_replace" and "commit.state" in point.args_summary
    ]
    assert len(publication) == 1, points
    database.close()
    release_ports(registry)

    registry, bench, inner = bench_registry(seed)
    database = connect(":memory:", registry=registry)
    schema(database)
    database.checkpoint()
    state_file_before = file_bytes(inner, "control/commit.state")
    log_before = {
        name: file_bytes(inner, name)
        for name in durable_names(inner)
        if name.startswith("wal/")
    }
    bench.clear_trail()
    bench.crash_at(publication[0])
    with pytest.raises(SimulatedCrash):
        insert(database, 2)
    bench.disarm()
    # A72: the state the probe needs, read from the DEVICE (what a reopen sees): the published
    # position did not move, the log did. The crashed handle's in-memory view is not the truth.
    assert file_bytes(inner, "control/commit.state") == state_file_before
    assert any(
        file_bytes(inner, name) != content for name, content in log_before.items()
    ) or any(
        name not in log_before
        for name in durable_names(inner)
        if name.startswith("wal/")
    )
    return registry, bench, inner


def lose_catalog_pages_after_a_ddl(seed: int) -> tuple[Any, Any, Any, bool]:
    """Commit a DDL with the bench reordering, then a power loss; say whether catalog pages went."""
    registry, bench, inner = bench_registry(seed)
    database = connect(":memory:", registry=registry)
    database.checkpoint()
    bench.start_reordering()
    catalog_before = file_bytes(inner, CATALOG_FILE)
    schema(database)
    catalog_after = file_bytes(inner, CATALOG_FILE)
    durable = {name: file_bytes(inner, name) for name in durable_names(inner)}
    power_loss(bench)
    catalog_now = file_bytes(inner, CATALOG_FILE)
    lost = catalog_now != catalog_after and catalog_before is not None
    kept = all(file_bytes(inner, n) == b for n, b in durable.items())
    return registry, bench, inner, bool(lost and kept)
