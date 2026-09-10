"""Read views: typed definitions, native snapshots and fail-closed metadata."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.model.value import ValueType
from okto_grafx.errors import GrafxError, GrafxLedgerError
from okto_grafx.views import ViewParameter


@pytest.fixture
def db(tmp_path):
    with connect(tmp_path / "views") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, body STRING, PRIMARY KEY(id))")
            tx.execute("CREATE (:N {id:1,body:'a'})")
            tx.execute("CREATE (:N {id:2,body:'b'})")
        db.views.prepare()
        yield db


def test_create_typed_execute_and_snapshot_replace_drop(db):
    definition = db.views.create(
        "by_id",
        query="MATCH (n:N) WHERE n.id=$id RETURN n.body",
        parameters_schema=(ViewParameter("id", ValueType.INT64),),
    )
    assert definition.columns == ("n.body",)
    assert tuple(n for n, _ in definition.dependencies) == ("N",)
    assert db.views.execute("by_id", {"id": 1}).rows == (("a",),)
    with db.begin("read") as old:
        replacement = db.views.create(
            "by_id",
            query="MATCH (n:N) WHERE n.id=$id RETURN n.id",
            parameters_schema=definition.parameters_schema,
            replace=True,
        )
        assert replacement.sha256 != definition.sha256
        assert db.views.get("by_id", snapshot=old) == definition
        assert db.views.execute("by_id", {"id": 1}, snapshot=old).rows == (("a",),)
        assert db.views.execute("by_id", {"id": 1}).rows == ((1,),)
        assert db.views.drop("by_id")
        assert db.views.get("by_id") is None
        assert db.views.get("by_id", snapshot=old) == definition


@pytest.mark.parametrize(
    "query",
    [
        "CREATE (:N {id:9})",
        "MATCH (n:N) DELETE n",
        "MATCH (n:N) SET n.body='oops' RETURN n.body",
        "CREATE NODE TABLE Other(id INT64, PRIMARY KEY(id))",
        "MATCH (n) RETURN n",
        "CALL show_tables() RETURN *",
        "MATCH (v:_grafx_views_v1) RETURN v.name",
        "RETURN 1 UNION ALL CREATE (:N {id:9}) RETURN 1",
    ],
)
def test_write_and_implicit_dependencies_refused_before_effects(db, query):
    before = db.transactions.published_state().last_committed_lsn
    with pytest.raises(GrafxError):
        db.views.create("unsafe", query=query)
    assert db.transactions.published_state().last_committed_lsn == before
    assert db.views.get("unsafe") is None


@pytest.mark.parametrize(
    "parameters", [{}, {"id": "1"}, {"id": True}, {"id": None}, {"id": 1, "extra": 2}]
)
def test_exact_parameter_contract(db, parameters):
    db.views.create(
        "typed",
        query="RETURN $id AS id",
        parameters_schema=(ViewParameter("id", ValueType.INT64),),
    )
    with pytest.raises(GrafxError):
        db.views.execute("typed", parameters)


def test_introspection_pages_prepare_idempotent_missing_drop(db):
    for name in ("a", "c", "b"):
        db.views.create(name, query="MATCH (n:N) RETURN n.id ORDER BY n.id")
    with db.begin("read") as tx:
        assert tuple(v.name for v in db.views.list(limit=2, snapshot=tx)) == ("a", "b")
        assert tuple(
            v.name for v in db.views.list(after="b", limit=2, snapshot=tx)
        ) == ("c",)
    before = db.transactions.published_state().last_committed_lsn
    db.views.prepare()
    assert not db.views.drop("missing")
    assert db.transactions.published_state().last_committed_lsn == before


def test_corrupt_definition_and_owner_refused(db):
    db.views.create("a", query="RETURN 1 AS n")
    with db.begin() as tx:
        tx.execute("MATCH (v:_grafx_views_v1) WHERE v.name='v_a' SET v.body='[]'")
    with pytest.raises(GrafxLedgerError):
        db.views.execute("a")
    with db.begin() as tx:
        tx.execute("MATCH (v:_grafx_views_v1) WHERE v.name='_owner' SET v.body='fake'")
    with pytest.raises(GrafxLedgerError):
        db.views.list()


def test_checkpoint_read_only_reopen(tmp_path):
    path = tmp_path / "reopen"
    with connect(path) as db:
        db.views.prepare()
        db.views.create("constant", query="RETURN 42 AS n")
        db.maintenance.checkpoint()
    with connect(path, read_only=True) as db:
        assert db.views.execute("constant").rows == ((42,),)
        with pytest.raises(GrafxError):
            db.views.create("forbidden", query="RETURN 0 AS n")


def test_invalid_arguments_and_failed_replace_preserves_old(db):
    prior = db.views.create("constant", query="RETURN 1 AS n")
    for kwargs in (
        {"query": "RETURN $missing"},
        {"query": "RETURN 1", "replace": 1},
        {"query": "x" * 65537},
    ):
        with pytest.raises(GrafxError):
            db.views.create("constant", **kwargs)
    assert db.views.get("constant") == prior
    with pytest.raises(GrafxError):
        db.views.create("bad-name", query="RETURN 1")
    with pytest.raises(GrafxError):
        db.views.list(limit=True)
    with db.begin() as writer:
        with pytest.raises(GrafxError):
            db.views.execute("constant", snapshot=writer)


@pytest.mark.parametrize(
    "cut,code,expected",
    [("before_commit", 71, 1), ("after_commit", 72, 2), ("before_apply", 73, 2)],
)
def test_process_death_recovery_atomic_replace(tmp_path, cut, code, expected):
    import os
    from pathlib import Path
    import subprocess
    import sys

    path = tmp_path / "crash"
    with connect(path) as db:
        db.views.prepare()
        db.views.create("value", query="RETURN 1 AS n")
        db.checkpoint()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    run = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("logical_views_worker.py")),
            str(path),
            cut,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == code, run.stderr
    for _ in range(2):
        with connect(path) as db:
            assert db.views.execute("value").rows == ((expected,),)
            assert not db.verify("all").findings
            db.checkpoint()


def test_concurrent_create_one_definition_and_independent_reader(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    db.views.create("visible", query="MATCH (n:N) RETURN n.id ORDER BY n.id")
    barrier = Barrier(2)

    def create(number):
        with connect(db.path) as participant:
            barrier.wait(timeout=15)
            try:
                return participant.views.create("race", query=f"RETURN {number} AS n")
            except GrafxError as error:
                return error

    with db.begin("read") as snapshot:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, (10, 20)))
        assert sum(not isinstance(r, Exception) for r in results) == 1
        assert db.views.get("race", snapshot=snapshot) is None
        assert db.views.execute("visible", snapshot=snapshot).rows == ((1,), (2,))
    assert len(db.views.list()) == 2


def test_stale_dependency_refused_even_with_valid_checksum(db):
    import json
    import hashlib

    definition = db.views.create("n", query="MATCH (n:N) RETURN n.id")
    body = json.dumps(
        [1, definition.query, [], definition.columns, [["N", "0" * 64]]],
        sort_keys=True,
        separators=(",", ":"),
    )
    with db.begin() as tx:
        tx.execute(
            "MATCH (v:_grafx_views_v1) WHERE v.name='v_n' SET v.body=$body,v.sha256=$hash",
            {"body": body, "hash": hashlib.sha256(body.encode("ascii")).hexdigest()},
        )
    with pytest.raises(GrafxLedgerError, match="Logical view refused"):
        db.views.execute("n")


def test_union_edge_view_and_unrelated_ddl(db):
    with db.begin() as tx:
        tx.execute("CREATE REL TABLE R(FROM N TO N, label STRING)")
        tx.execute(
            "MATCH (a:N),(b:N) WHERE a.id=1 AND b.id=2 CREATE (a)-[:R {label:'edge'}]->(b)"
        )
    db.views.create("edges", query="MATCH (a:N)-[r:R]->(b:N) RETURN a.id,b.id,r.label")
    union = db.views.create(
        "union_values",
        query="MATCH (n:N) WHERE n.id=1 RETURN n.id UNION ALL MATCH (n:N) WHERE n.id=2 RETURN n.id",
    )
    assert db.views.execute("edges").rows == ((1, 2, "edge"),)
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE Unrelated(id INT64, PRIMARY KEY(id))")
    assert db.views.get("union_values") == union
    assert db.views.execute("union_values").rows == ((1,), (2,))
