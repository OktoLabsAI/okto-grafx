"""Existing-target copy: native atomicity, endpoint remap and durable indexed receipts."""

from dataclasses import replace

import pytest

from okto_grafx import connect, CommitMetadata
from okto_grafx.catalog_copy import (
    capture_copy,
    copy_graph,
    prepare_copy_target,
    CopyLimits,
)
from okto_grafx.errors import GrafxError, GrafxConfigurationError, GrafxLedgerError


def schema(db):
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE N(id INT64, body STRING, PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE R(FROM N TO N, label STRING)")


@pytest.fixture
def setup(tmp_path):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        source.ensure_identity_indexes()
        source.enable_commit_history()
        schema(source)
        schema(target)
        prepare_copy_target(target)
        with source.begin(metadata=CommitMetadata(actor="source")) as tx:
            tx.execute("CREATE (:N {id:1, body:'one'})")
            tx.execute("CREATE (:N {id:2, body:'two'})")
            tx.execute(
                "MATCH (a:N),(b:N) WHERE a.id=1 AND b.id=2 CREATE (a)-[:R {label:'r'}]->(b)"
            )
        with source.begin("read") as tx:
            package = capture_copy(tx, tables=("N", "R"))
        yield source, target, package


def test_copy_remaps_edges_and_replay_is_read_only(setup):
    _, target, package = setup
    with target.begin() as tx:
        tx.execute("CREATE (:N {id:100, body:'existing'})")
    result = copy_graph(package, target, idempotency_key="first")
    assert result.rows == 3 and not result.replayed
    assert target.execute("MATCH (a:N)-[r:R]->(b:N) RETURN a.id,b.id,r.label").rows == (
        (1, 2, "r"),
    )
    before = target.transactions.published_state().last_committed_lsn
    replay = copy_graph(package, target, idempotency_key="first")
    assert replay == replace(result, replayed=True)
    assert target.transactions.published_state().last_committed_lsn == before


def test_selected_capture_uses_rid_index_and_checks_endpoint_closure(setup, monkeypatch):
    from okto_grafx.engine.heap_store import HeapStore
    source, target, package = setup
    ids = {item.schema.name: tuple(rid for rid, _ in item.rows) for item in package.tables}
    with source.begin("read") as tx:
        monkeypatch.setattr(HeapStore, "scan", lambda *_a, **_k: pytest.fail("Selected capture must not scan heap"))
        selected = capture_copy(tx, tables=("N", "R"), record_ids=ids)
        assert selected == package
        with pytest.raises(GrafxConfigurationError):
            capture_copy(tx, tables=("N", "R"), record_ids={"N": ids["N"][:1], "R": ids["R"]})
        with pytest.raises(GrafxConfigurationError):
            capture_copy(tx, tables=("N",), record_ids={"N": (2**63,)})
    monkeypatch.undo()
    assert copy_graph(selected, target, idempotency_key="subset").rows == 3


def test_skip_conflicts_preserves_existing_nodes_remaps_edges_and_replays(setup):
    _, target, package = setup
    with target.begin() as tx:
        tx.execute("CREATE (:N {id:1, body:'keep-target'})")
    receipt = copy_graph(package, target, idempotency_key="skip", conflict="skip")
    assert receipt.rows == 2 and receipt.skipped_rows == 1
    assert target.execute("MATCH (n:N {id:1}) RETURN n.body").rows == (("keep-target",),)
    assert target.execute("MATCH (a:N)-[r:R]->(b:N) RETURN a.id,b.id").rows == ((1, 2),)
    assert copy_graph(package, target, idempotency_key="skip", conflict="skip") == replace(receipt, replayed=True)
    with pytest.raises(GrafxLedgerError):
        copy_graph(package, target, idempotency_key="skip", conflict="fail")
    target.checkpoint()
    path = target.path
    target.close()
    with connect(path) as reopened:
        assert copy_graph(package, reopened, idempotency_key="skip", conflict="skip") == replace(receipt, replayed=True)
        assert reopened.verify().clean


def test_same_key_different_request_and_mutated_package_refused(setup):
    _, target, package = setup
    copy_graph(package, target, idempotency_key="first")
    with pytest.raises(GrafxLedgerError):
        copy_graph(
            package,
            target,
            idempotency_key="first",
            metadata=CommitMetadata(actor="changed"),
        )
    with pytest.raises(GrafxConfigurationError):
        copy_graph(replace(package, sha256="0" * 64), target, idempotency_key="other")


def test_capture_is_detached_and_conflict_rolls_back(setup):
    source, target, package = setup
    with source.begin() as tx:
        tx.execute("MATCH (n:N) WHERE n.id=1 SET n.body='changed'")
    copy_graph(package, target, idempotency_key="first")
    assert target.execute("MATCH (n:N) WHERE n.id=1 RETURN n.body").rows == (("one",),)
    with pytest.raises(GrafxError):
        copy_graph(package, target, idempotency_key="conflicting")
    assert target.execute("MATCH (r:_grafx_copy_receipts_v1) RETURN count(*)").rows == (
        (2,),
    )
    assert target.execute("MATCH (n:N) RETURN count(*)").rows == ((2,),)


def test_reopen_and_indexed_receipt_proof(setup):
    _, target, package = setup
    first = copy_graph(package, target, idempotency_key="reopen")
    target.checkpoint()
    path = target.path
    target.close()
    with connect(path) as reopened:
        assert copy_graph(package, reopened, idempotency_key="reopen") == replace(
            first, replayed=True
        )
        assert not reopened.verify("all").findings


def test_unsupported_or_unprepared_target_refused(setup, tmp_path):
    source, target, package = setup
    with pytest.raises(GrafxError):
        copy_graph(package, source, idempotency_key="self")
    with pytest.raises(GrafxError):
        copy_graph(package, target, idempotency_key="bad", conflict="merge")
    with connect(tmp_path / "unprepared") as db:
        with pytest.raises(GrafxError):
            copy_graph(package, db, idempotency_key="bad")
        assert not db.catalog.catalog.tables()


def test_capture_bounds_and_endpoint_closure(setup):
    source, _, _ = setup
    with source.begin("read") as tx:
        with pytest.raises(GrafxConfigurationError):
            capture_copy(tx, tables=("N", "R"), limits=CopyLimits(max_rows=1))
    with source.begin("read") as tx:
        with pytest.raises(GrafxConfigurationError):
            capture_copy(tx, tables=("R",))


def test_receipt_mutation_is_not_proof(setup):
    _, target, package = setup
    copy_graph(package, target, idempotency_key="first")
    # Even unchanged values written in a later, unrelated COMMIT invalidate proof.
    with target.begin() as tx:
        tx.execute(
            "MATCH (r:_grafx_copy_receipts_v1) WHERE r.key='request:first' SET r.rows=3"
        )
    with pytest.raises(GrafxLedgerError):
        copy_graph(package, target, idempotency_key="first")


@pytest.mark.parametrize(
    "cut,code,committed",
    [
        ("before_commit", 71, False),
        ("after_commit", 72, True),
        ("before_heap_apply", 73, True),
    ],
)
def test_process_death_and_recovery(setup, cut, code, committed):
    import os
    from pathlib import Path
    import subprocess
    import sys

    source, target, package = setup
    source.checkpoint()
    target.checkpoint()
    source.close()
    target.close()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    run = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("catalog_copy_worker.py")),
            source.path,
            target.path,
            cut,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert run.returncode == code, run.stderr
    with connect(target.path) as recovered:
        assert recovered.execute("MATCH (n:N) RETURN count(*)").rows == (
            (2 if committed else 0,),
        )
        receipt = copy_graph(package, recovered, idempotency_key="crash")
        assert receipt.replayed is committed
        assert recovered.execute("MATCH (a:N)-[r:R]->(b:N) RETURN a.id,b.id").rows == (
            (1, 2),
        )
        assert not recovered.verify("all").findings
        recovered.checkpoint()
    with connect(target.path) as reopened:
        assert copy_graph(package, reopened, idempotency_key="crash") == replace(
            receipt, replayed=True
        )


def test_receipt_lookup_does_not_scan_history_or_heap(setup, monkeypatch):
    from okto_grafx.engine.heap_store import HeapStore
    from okto_grafx.engine.commit_catalog_store import CommitCatalogStore

    _, target, package = setup
    first = copy_graph(package, target, idempotency_key="indexed")

    def refuse(*args, **kwargs):
        raise AssertionError("receipt lookup cannot scan a heap or commit history")

    monkeypatch.setattr(HeapStore, "scan", refuse)
    monkeypatch.setattr(HeapStore, "_walk", refuse)
    monkeypatch.setattr(CommitCatalogStore, "history", refuse)
    assert copy_graph(package, target, idempotency_key="indexed") == replace(
        first, replayed=True
    )


def test_concurrent_identical_requests_leave_one_outcome(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    _, target, package = setup
    barrier = Barrier(2)

    def apply():
        with connect(target.path) as participant:
            barrier.wait(timeout=15)
            try:
                return copy_graph(package, participant, idempotency_key="concurrent")
            except GrafxError as error:
                return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: apply(), range(2)))
    assert any(not isinstance(r, Exception) for r in outcomes)
    recovered = copy_graph(package, target, idempotency_key="concurrent")
    assert recovered.replayed
    assert target.execute("MATCH (n:N) RETURN count(*)").rows == ((2,),)
    assert target.execute("MATCH (r:_grafx_copy_receipts_v1) RETURN count(*)").rows == (
        (2,),
    )


def test_preparation_idempotent_and_ledger_owner_checked(setup):
    _, target, _ = setup
    before = target.transactions.published_state().last_committed_lsn
    prepare_copy_target(target)
    assert target.transactions.published_state().last_committed_lsn == before
    with target.begin() as tx:
        tx.execute(
            "MATCH (r:_grafx_copy_receipts_v1) WHERE r.key='owner' SET r.target='wrong'"
        )
    with pytest.raises(GrafxLedgerError):
        prepare_copy_target(target)


@pytest.mark.parametrize("key", ["", "x" * 129, "a/b", "é", None])
def test_key_admission_before_target_write(setup, key):
    _, target, package = setup
    before = target.transactions.published_state().last_committed_lsn
    with pytest.raises(GrafxConfigurationError):
        copy_graph(package, target, idempotency_key=key)
    assert target.transactions.published_state().last_committed_lsn == before


def test_vector_space_ids_remap_and_null_values(setup):
    source, target, _ = setup
    with target.begin() as tx:
        tx.execute("CREATE VECTOR SPACE Padding {dimension:2,metric:'cosine'}")
    for db in (source, target):
        with db.begin() as tx:
            tx.execute("CREATE VECTOR SPACE Vec {dimension:2,metric:'cosine'}")
            tx.execute(
                "CREATE NODE TABLE V(id INT64, v VECTOR(Vec), note STRING, PRIMARY KEY(id))"
            )
    with source.begin() as tx:
        tx.execute("CREATE (:V {id:1,v:[1.0,0.0]})")
    with source.begin("read") as tx:
        package = capture_copy(tx, tables=("V",))
    receipt = copy_graph(package, target, idempotency_key="vector")
    assert receipt.rows == 1
    rows = target.execute("MATCH (n:V) RETURN n.v,n.note").rows
    assert rows[0][0].space_ref == target.catalog.catalog.space("Vec").space_id
    assert rows[0][1] is None
    assert not target.verify("all").findings


def test_session_permission_and_registered_copy_lifetime(setup, tmp_path, monkeypatch):
    from okto_grafx import CatalogSession, CatalogPathPolicy, Transaction
    from okto_grafx.errors import GrafxTransactionStateError

    source, target, package = setup
    with CatalogSession(
        source, owned=False, policy=CatalogPathPolicy((str(tmp_path),))
    ) as session:
        session.attach_handle(target, alias="target", owned=False)
        with pytest.raises(GrafxTransactionStateError):
            session.apply_copy(package, target="target", idempotency_key="session")
        session.detach("target")
        session.attach_handle(target, alias="target", owned=False, read_only=False)
        native_commit = Transaction.commit

        def commit(tx):
            if tx._database is target:
                with pytest.raises(GrafxTransactionStateError):
                    session.detach("target")
            return native_commit(tx)

        monkeypatch.setattr(Transaction, "commit", commit)
        assert not session.apply_copy(
            package, target="target", idempotency_key="session"
        ).replayed
        assert session.apply_copy(
            package, target="target", idempotency_key="session"
        ).replayed


def test_target_schema_mismatch_and_non_pk_source_capture(setup, tmp_path):
    source, _, package = setup
    with connect(tmp_path / "incompatible") as target:
        prepare_copy_target(target)
        with target.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, other STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N, label STRING)")
        with pytest.raises(GrafxConfigurationError):
            copy_graph(package, target, idempotency_key="mismatch")
        assert target.execute("MATCH (n:N) RETURN count(*)").rows == ((0,),)
    with source.begin() as tx:
        tx.execute("CREATE NODE TABLE Anonymous(value STRING)")
    with source.begin("read") as tx:
        captured = capture_copy(tx, tables=("Anonymous",))
        assert captured.tables[0].schema.primary_key is None


def test_empty_relationship_properties(setup):
    source, target, _ = setup
    for db in (source, target):
        with db.begin() as tx:
            tx.execute("CREATE REL TABLE S(FROM N TO N)")
    with source.begin() as tx:
        tx.execute("MATCH (a:N),(b:N) WHERE a.id=1 AND b.id=2 CREATE (a)-[:S]->(b)")
    with source.begin("read") as tx:
        package = capture_copy(tx, tables=("N", "S"))
    assert copy_graph(package, target, idempotency_key="empty_props").rows == 3
    assert target.execute("MATCH (a:N)-[:S]->(b:N) RETURN a.id,b.id").rows == ((1, 2),)
