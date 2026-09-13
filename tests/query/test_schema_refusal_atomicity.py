"""A refused schema STATEMENT leaves the transaction exactly as it found it -- the hold doctrine.

Round-5 blind review, two blocking defects, one root: the schema path did not hold until
complete. `add_table` mutated the transaction's remembered working copy IN PLACE, and the index
attaches install state outside the transaction (the registry, the vector engine's space map, the
index FILES), so a refusal part-way left all of it behind. Measured through `connect()` alone:

* a `CREATE NODE TABLE person(...)` refused by the case-folded index name left the phantom table
  in the working copy; `CREATE (:person {id: 1})` in the same transaction was ACCEPTED, and the
  commit made durable rows for a table no catalog would ever describe -- unreachable, unreported,
  `verify()` clean;
* `db.retry()` of a conflicted DDL never settled the loser, so the successor's re-executed DDL
  was refused by its own predecessor's leftovers -- first the registration, then (with the
  registration pruned) the orphan index FILE, whose digest can never match the successor's
  definition because the ids re-derive from pages the winner changed;
* `db.execute("CREATE TABLE ...")` -- the read door, an ordinary misuse -- registered indexes
  FIRST and refused second, poisoning the process the same way.

The fix: the statement works on a CLONE adopted only at the end; every out-of-transaction effect
is journalled (index registered, space attached, skip reported, FILE created) and a refusal
replays the journal in reverse; `Database.retry` and rollback replay the TRANSACTION's journal.
The journal is by NAME, never a prune against a catalog -- a catalog prune was tried first and
fails exactly when it matters, because a loser's ids and the winner's ids come from the same
committed pages, and another open transaction's attachments are absent from every catalog this
one can see.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxIndexError,
    GrafxTransactionStateError,
    GrafxWriteConflict,
)
from okto_grafx.engine.query_engine import QueryEngine


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_a_refused_statement_poisons_nothing_and_the_transaction_still_commits(
    tmp_path: Path, monkeypatch, codec,
) -> None:
    """A late failure after actual index attachment must unwind the statement.

    The original reproduction relied on a case-folded name collision. Physical
    owner identities now allow those names in catalog v2, so inject a refusal
    after real attachment and prove exact retry, prior writes and cold reopen.
    """
    root = tmp_path / "db"
    with okto_grafx.connect(root, page_size=512, codec=codec) as db:
        db.ensure_identity_indexes()
        txn = db.begin("write")
        txn.execute("CREATE VECTOR SPACE s {dimension: 4, metric: 'cosine'}")
        txn.execute("CREATE NODE TABLE Person(id INT64, body VECTOR(s), PRIMARY KEY(id))")
        original = QueryEngine._attach_vector_columns
        attached = []

        def fail_after_attach(self, table, *args, **kwargs):
            result = original(self, table, *args, **kwargs)
            if table.name == "person":
                attached.append(table.table_id)
                raise GrafxIndexError("Injected failure after vector attachment.", field="late_ddl_probe")
            return result

        # Case-distinct physical owners are legal in catalog v2. Preserve the
        # original late-refusal invariant with an actual post-attachment fault,
        # not an expectation that the broader schema must still reject them.
        with monkeypatch.context() as patch:
            patch.setattr(QueryEngine, "_attach_vector_columns", fail_after_attach)
            with pytest.raises(GrafxIndexError) as failure:
                txn.execute("CREATE NODE TABLE person(id INT64, body VECTOR(s), PRIMARY KEY(id))")
        assert failure.value.details["field"] == "late_ddl_probe"
        assert len(attached) == 1

        # An absent-label CREATE is now a legitimate schema-free operation, so
        # it is no longer evidence of a leaked typed table. Exact typed DDL on
        # retry proves the failed statement did not keep its table or indexes.
        txn.execute("CREATE NODE TABLE person(id INT64, body VECTOR(s), PRIMARY KEY(id))")
        txn.execute("CREATE (:person {id: 1, body: [0.0, 1.0, 0.0, 0.0]})")
        txn.execute("CREATE (:Person {id: 7, body: [1.0, 0.0, 0.0, 0.0]})")
        txn.commit()

        assert db.execute("MATCH (p:Person) RETURN p.id").rows == ((7,),)
        assert db.execute("MATCH (p:person) RETURN p.id").rows == ((1,),)
        assert sorted(t.name for t in db.catalog.catalog.tables()) == ["Person", "person"]
        assert db.verify("all").findings == ()

    with okto_grafx.connect(root, page_size=512) as reopened:
        assert sorted(t.name for t in reopened.catalog.catalog.tables()) == ["Person", "person"]
        assert reopened.execute("MATCH (p:person) RETURN p.id").rows == ((1,),)
        assert reopened.execute("MATCH (p:Person) RETURN p.id").rows == ((7,),)
        assert reopened.verify("all").findings == ()


def test_legacy_case_collision_refuses_before_adopting_a_phantom_table(tmp_path):
    with okto_grafx.connect(tmp_path / "legacy", page_size=512) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE VECTOR SPACE s {dimension: 4, metric: 'cosine'}")
            txn.execute("CREATE NODE TABLE Person(id INT64, body VECTOR(s), PRIMARY KEY(id))")
            with pytest.raises(GrafxConfigurationError) as failure:
                txn.execute("CREATE NODE TABLE person(id INT64, body VECTOR(s), PRIMARY KEY(id))")
            assert failure.value.details["field"] == "format_version"
            txn.execute("CREATE (:Person {id: 7, body: [1.0, 0.0, 0.0, 0.0]})")
        assert sorted(t.name for t in db.catalog.catalog.tables()) == ["Person"]
        assert db.execute("MATCH (p:Person) RETURN p.id").rows == ((7,),)
        assert not db.verify("all").findings


def test_retry_settles_the_loser_so_the_successor_can_re_execute_its_ddl(
    tmp_path: Path,
) -> None:
    """The documented BR-6 loop must be able to SUCCEED for a schema change.

    Before the fix the successor was refused twice over: by the loser's surviving registration,
    and -- with that pruned -- by the loser's orphan index FILE, whose definition digest can
    never match the successor's (the ids re-derive from pages the winner changed). The journal
    therefore also records the files a statement's registrations CREATED, and the unwind removes
    exactly those, frames first so a later flush cannot re-create them.
    """
    with okto_grafx.connect(tmp_path / "db", page_size=512) as db:
        loser = db.begin("write")
        loser.execute("CREATE VECTOR SPACE s {dimension: 4, metric: 'cosine'}")
        loser.execute("CREATE NODE TABLE V(id INT64, body VECTOR(s), PRIMARY KEY(id))")
        with db.begin("write") as winner:
            winner.execute("CREATE NODE TABLE Other(id INT64, PRIMARY KEY(id))")
        with pytest.raises(GrafxWriteConflict):
            loser.commit()

        successor = db.retry(loser)
        successor.execute("CREATE VECTOR SPACE s {dimension: 4, metric: 'cosine'}")
        successor.execute("CREATE NODE TABLE V(id INT64, body VECTOR(s), PRIMARY KEY(id))")
        successor.commit()

        assert sorted(t.name for t in db.catalog.catalog.tables()) == ["Other", "V"]
        assert db.vectors.index("s").name == "vector_V_s"
        assert db.verify("all").findings == ()


def test_the_read_door_refuses_a_schema_statement_before_any_side_effect(
    tmp_path: Path,
) -> None:
    """Refused by MODE, up front -- not by staging, which comes after the registrations.

    The old guard checked that `stage_page_image` was callable, which a read context satisfies;
    its refusal came when the method was CALLED, after the index registrations, and the
    legitimate write-door retry of the same DDL was then refused for the life of the process.
    """
    with okto_grafx.connect(tmp_path / "db", page_size=512) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE VECTOR SPACE s {dimension: 4, metric: 'cosine'}")

        with pytest.raises(GrafxTransactionStateError):
            db.execute("CREATE NODE TABLE V(id INT64, body VECTOR(s), PRIMARY KEY(id))")
        assert [index.name for index in db.indexes.indexes()] == []

        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE V(id INT64, body VECTOR(s), PRIMARY KEY(id))")
        assert sorted(index.name for index in db.indexes.indexes()) == [
            "pk_V",
            "vector_V_s",
        ]


def test_a_rolled_back_ddl_releases_indexes_without_blocking_a_retry(
    tmp_path: Path,
) -> None:
    """Rollback releases public/registry ownership and preserved bytes remain reclaimable.

    Canonical deletion cannot be a cross-process compare-and-swap: another speculative DDL may
    already have adopted the same bytes.  Rollback therefore removes only the exact local
    registration/map claims. A later incompatible definition must move the preserved artifacts
    aside under the artifact section and remain correct both live and after a cold reopen.
    """
    root = tmp_path / "db"
    with okto_grafx.connect(root, page_size=512) as db:
        doomed = db.begin("write")
        doomed.execute("CREATE VECTOR SPACE s {dimension: 4, metric: 'cosine'}")
        doomed.execute("CREATE NODE TABLE V(id INT64, body VECTOR(s), PRIMARY KEY(id))")
        doomed.rollback()
        assert db.indexes.indexes() == ()
        assert db.vectors.spaces() == ()
        assert db.vectors.indexes() == ()
        assert db.storage.exists("index/vector_V_s.idx")
        assert db.storage.exists("index/pk_V.idx")
        assert db.verify("all").findings == ()

        with db.begin("write") as retry:
            retry.execute(
                "CREATE VECTOR SPACE s {dimension: 3, metric: 'euclidean', "
                "storage_dtype: 'float64'}"
            )
            retry.execute(
                "CREATE NODE TABLE V("
                "business_key STRING, id INT64, body VECTOR(s), PRIMARY KEY(id))"
            )
        with db.begin("write") as writer:
            writer.execute(
                "CREATE (:V {business_key: 'new', id: 1, body: [1.0, 0.0, 0.0]})"
            )
        assert db.indexes.index("pk_V").definition.positions == (1,)
        assert db.indexes.index("vector_V_s").definition.positions == (2,)
        vector = db.vectors.index("s")
        assert (vector.dimension, vector.metric_of_space.value, vector.storage_dtype) == (
            3,
            "euclidean",
            "float64",
        )
        assert db.execute(
            "MATCH (v:V) WHERE v.id = 1 RETURN v.business_key, v.id"
        ).rows == (("new", 1),)
        assert db.verify("all").findings == ()

    with okto_grafx.connect(root, page_size=512) as cold:
        assert cold.indexes.index("pk_V").definition.positions == (1,)
        assert cold.indexes.index("vector_V_s").definition.positions == (2,)
        vector = cold.vectors.index("s")
        assert (vector.dimension, vector.metric_of_space.value, vector.storage_dtype) == (
            3,
            "euclidean",
            "float64",
        )
        assert cold.execute(
            "MATCH (v:V) WHERE v.id = 1 RETURN v.business_key, v.id"
        ).rows == (("new", 1),)
        assert cold.verify("all").findings == ()
