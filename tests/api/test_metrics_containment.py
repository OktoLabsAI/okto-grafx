"""A host-supplied metrics sink may do anything at all, and none of it leaves a public door.

C8's blind review, one blocking defect (C8-B1). The registry validates a sink's SHAPE and cannot
validate its behaviour, and the engine records metrics from inside public doors -- including
AFTER a commit's invariants have settled. A sink that raised on the post-commit gauge made a
DURABLY COMMITTED transaction report failure, and the caller's ordinary reaction -- retry the
"failed" statement -- duplicated the row. The composition already contains foreign exceptions at
`connect` ("only Grafx types leave a public door"); `ContainedMetricsSink` is the same rule
applied to every recording call for the life of the database. Telemetry is never load-bearing
(G7 freezes the CATALOGUE, not the delivery), so a recording failure is absorbed; `publish` is
the one door whose failure a caller acts on, and it stays typed instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import okto_grafx
from okto_grafx import DatabaseConfig
from okto_grafx.runtime.bootstrap import build_default_registry


class _HostileSink:
    """Raises on every door a sink has, including the property."""

    calls: int = 0

    @property
    def enabled(self) -> bool:
        raise RuntimeError("host sink boom: enabled")

    def register(self, descriptor) -> None:
        raise RuntimeError("host sink boom: register")

    def increment(self, name, value=1.0, labels=None) -> None:
        raise RuntimeError("host sink boom: increment")

    def set_gauge(self, name, value, labels=None) -> None:
        raise RuntimeError("host sink boom: set_gauge")

    def observe(self, name, value, labels=None) -> None:
        raise RuntimeError("host sink boom: observe")

    def time(self, name, labels=None):
        raise RuntimeError("host sink boom: time")

    def snapshot(self):
        raise RuntimeError("host sink boom: snapshot")


class _PostCommitBomb(_HostileSink):
    """Behaves until the write-gauge falls -- the post-commit site -- then raises for ever.

    The precise shape of C8-B1: everything up to and through the barrier succeeds, the raise
    lands after durability, and the report used to become a lie.
    """

    def __init__(self) -> None:
        self.armed = False

    @property
    def enabled(self) -> bool:
        return True

    def register(self, descriptor) -> None:
        return

    def increment(self, name, value=1.0, labels=None) -> None:
        if self.armed:
            raise RuntimeError("post-commit boom")

    def set_gauge(self, name, value, labels=None) -> None:
        if name == "oktografx_active_transactions" and value == 0.0:
            self.armed = True
            raise RuntimeError("post-commit boom")
        if self.armed:
            raise RuntimeError("post-commit boom")

    def observe(self, name, value, labels=None) -> None:
        if self.armed:
            raise RuntimeError("post-commit boom")

    def time(self, name, labels=None):
        from contextlib import nullcontext

        return nullcontext()

    def snapshot(self):
        return {}


def _database_with(tmp_path: Path, sink):
    root = str(tmp_path / "db")
    registry = build_default_registry(DatabaseConfig(path=root))
    registry.bind("metrics", sink)
    return okto_grafx.connect(root, registry=registry)


def test_a_sink_that_always_raises_breaks_nothing(tmp_path: Path) -> None:
    db = _database_with(tmp_path, _HostileSink())
    try:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE T(id INT64, n STRING, PRIMARY KEY(id))")
        with db.begin("write") as txn:
            txn.execute("CREATE (:T {id: 1, n: 'boom'})")
        assert db.execute("MATCH (t:T) RETURN t.id, t.n").rows == ((1, "boom"),)
        assert db.verify("all").findings == ()
    finally:
        db.close()


def test_a_post_commit_raise_cannot_make_a_durable_commit_report_failure(
    tmp_path: Path,
) -> None:
    """The duplication shape, held shut.

    Before the containment: `txn.commit()` raised AFTER durability, the caller retried the
    "failed" CREATE, and the table held the row twice. Now the commit returns its report, and
    the row is there exactly once.
    """
    db = _database_with(tmp_path, _PostCommitBomb())
    try:
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE T(id INT64, n STRING, PRIMARY KEY(id))")
        writer = db.begin("write")
        writer.execute("CREATE (:T {id: 1, n: 'once'})")
        report = writer.commit()          # must NOT raise, and must tell the truth
        assert report.durable
        assert db.execute("MATCH (t:T) RETURN count(*)").rows == ((1,),)
    finally:
        db.close()


def test_publish_stays_typed_and_the_json_selector_writes_its_file(tmp_path: Path) -> None:
    """The one metrics door a caller ACTS on keeps its contract, and close() now walks it.

    `metrics="json"` documents that the sink writes to the destination the configuration names,
    and nothing in the shipped composition ever called `publish()` -- the flag was silently
    inert, which the parser's own standard calls "a setting the operator believes they applied".
    """
    destination = tmp_path / "metrics.json"
    db = okto_grafx.connect(
        tmp_path / "db", metrics="json", metrics_destination=str(destination)
    )
    with db.begin("write") as txn:
        txn.execute("CREATE NODE TABLE T(id INT64, PRIMARY KEY(id))")
    db.close()
    assert destination.exists(), "close() did not publish the metrics document"
    assert json.loads(destination.read_text(encoding="utf-8"))
