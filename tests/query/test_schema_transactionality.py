"""A schema change is a transaction like any other: staged, committed, or GONE.

WHY THIS FILE EXISTS. The DDL path used `CatalogStore.save()`, which writes through the buffer
pool the moment it is called, and mutated the LIVE catalog at statement time. Measured through
`connect()` alone, that produced two failures the taxonomy never saw:

* **A rolled-back `CREATE TABLE` survived a REOPEN.** The rollback dropped the transaction's
  staged records, but the pool already held the catalog pages save() wrote, and the next flush of
  anyone carried them to the device: an uncommitted schema change made durable. In the same
  session, retrying the identical `CREATE` was refused with "already has a table named" -- the
  live catalog still held the ghost -- and the index the DDL registered stayed registered.

* **A committed `CREATE TABLE`'s file header reached the device outside every log record.**
  save() returns the CHAIN pages only, so the one page carrying `root_page` and `payload_length`
  was never staged, never logged, and unreachable to redo -- a crash between the commit's barrier
  and its flush lost the schema with no refusal anywhere (recorded in PUNCHLIST as "still live end
  to end"; the staged door existed and nothing walked through it).

The fix is the door `CatalogStore.stage()` already documented: every schema statement builds on a
per-transaction WORKING COPY of the catalog, stages the images -- chain, freed pages, and page 0,
unconditionally -- on the transaction, and the live catalog learns about the change when the
commit applies them. A rollback drops the copy and prunes the two side effects DDL makes outside
the transaction: the indexes it registered and the vector engine's space map.

The one behavior this changes on purpose: a table is visible to OTHER transactions only once the
transaction that declared it commits. Within the declaring transaction everything still works --
the second statement of the quick start's schema block sees the first one's tables, rows can be
written into a table declared moments earlier, and a REL TABLE can name endpoints from the same
block -- because planning and execution inside that transaction resolve names from its working
copy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.domain.txn import WalRecordType
from okto_grafx.domain.txn.records import decode_page_write


# --- the rollback half ---------------------------------------------------------------------------


def test_a_rolled_back_create_table_leaves_nothing_anywhere(tmp_path: Path) -> None:
    """The headline regression: catalog, registry, retry, and -- above all -- the reopen.

    Before the fix this failed at every assertion below: the table stayed in the live catalog,
    `pk_Ghost` stayed registered, the retry was refused with "already has a table named", and the
    ghost SURVIVED THE REOPEN because the pool carried save()'s pages to the device.
    """
    root = tmp_path / "db"
    with okto_grafx.connect(root, page_size=512) as db:
        doomed = db.begin("write")
        doomed.execute("CREATE NODE TABLE Ghost(id INT64, PRIMARY KEY(id))")
        doomed.rollback()

        assert not db.catalog.catalog.has_table("Ghost"), (
            "the live catalog holds a table whose transaction rolled back"
        )
        assert [index.name for index in db.indexes.indexes()] == [], (
            "the rolled-back DDL's index registration was not pruned"
        )

        with db.begin("write") as retry:
            retry.execute("CREATE NODE TABLE Ghost(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as writer:
            writer.execute("CREATE (:Ghost {id: 1})")
        assert db.execute("MATCH (g:Ghost) RETURN g.id").rows == ((1,),)

    with okto_grafx.connect(root, page_size=512) as reopened:
        assert sorted(t.name for t in reopened.catalog.catalog.tables()) == ["Ghost"]
        assert reopened.execute("MATCH (g:Ghost) RETURN g.id").rows == ((1,),)
        assert reopened.verify("all").findings == ()


def test_a_rolled_back_vector_space_is_pruned_from_the_vector_engine(tmp_path: Path) -> None:
    """attach() runs at statement time and installs per-space state OUTSIDE the transaction.

    Without the prune, `vectors.index("dead")` answered for a space that does not exist, and the
    retry of the same block was refused. The registry half is pruned by table id; this asserts
    the vector engine's own map, which is a separate structure and was a separate leak.
    """
    root = tmp_path / "db"
    with okto_grafx.connect(root, page_size=512) as db:
        doomed = db.begin("write")
        doomed.execute("CREATE VECTOR SPACE dead {dimension: 4, metric: 'cosine'}")
        doomed.execute("CREATE NODE TABLE D(id INT64, e VECTOR(dead), PRIMARY KEY(id))")
        doomed.rollback()

        with pytest.raises(GrafxIndexError):
            db.vectors.index("dead")
        assert [index.name for index in db.indexes.indexes()] == []

        with db.begin("write") as retry:
            retry.execute("CREATE VECTOR SPACE dead {dimension: 4, metric: 'cosine'}")
            retry.execute("CREATE NODE TABLE D(id INT64, e VECTOR(dead), PRIMARY KEY(id))")
        assert db.vectors.index("dead").name == "vector_D_dead"
        assert db.verify("all").findings == ()


def test_an_uncommitted_table_is_invisible_to_other_transactions(tmp_path: Path) -> None:
    """The behavior change, stated as a test rather than left to be discovered.

    While the declaring transaction is open, another transaction planning against the live
    catalog must not see the table -- seeing it was how a rollback left ghosts. The declaring
    transaction itself sees it, which the same-transaction test below holds.
    """
    with okto_grafx.connect(tmp_path / "db", page_size=512) as db:
        open_ddl = db.begin("write")
        open_ddl.execute("CREATE NODE TABLE Pending(id INT64, PRIMARY KEY(id))")
        open_ddl.execute("CREATE (:Pending {id:7})")
        try:
            # FP-3 absent-table reads return an empty relation, not an error.
            # An owner row distinguishes isolation from seeing a phantom empty table.
            assert db.execute("MATCH (p:Pending) RETURN count(*)").rows == ((0,),)
            assert not db.catalog.catalog.has_table("Pending")
            assert open_ddl.execute("MATCH (p:Pending) RETURN p.id").rows == ((7,),)
        finally:
            open_ddl.rollback()


# --- the durability half -------------------------------------------------------------------------


def test_a_committed_schema_change_logs_the_catalog_header_page(tmp_path: Path) -> None:
    """Page 0 of the catalog must be in the commit's WAL batch, or no redo can reach the schema.

    The header page is what makes the chain reachable -- `root_page` and `payload_length` live on
    it -- and save() returned the chain only, so the header reached the device through an
    ordinary pool flush, covered by no record. A crash between the barrier and the flush lost
    the schema: the log held the chain pages of a catalog whose header still named the old one.
    stage() returns page 0 unconditionally and the statement stages everything it returns.
    """
    with okto_grafx.connect(tmp_path / "db", page_size=512) as db:
        catalog_file = db.catalog.file
        before = db.wal.last_lsn
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE Team(id INT64, PRIMARY KEY(id))")

        logged = {
            decode_page_write(record.payload).page_index
            for record in db._wal.read_from(before + 1)
            if record.record_type == WalRecordType.WRITE_PAGE
            and decode_page_write(record.payload).file == catalog_file
        }
        assert 0 in logged, (
            f"the commit logged catalog pages {sorted(logged)} and not the header page, "
            f"so a replay rebuilds a catalog whose header names the old chain"
        )


# --- the same-transaction contract ---------------------------------------------------------------


def test_one_transaction_declares_a_schema_and_uses_it(tmp_path: Path) -> None:
    """The quick start's shape: space, tables, a rel table naming them, and rows -- one block.

    Every later statement resolves names from the transaction's working copy: the planner, the
    row materialiser (the vector column needs its space), and the traversal (the rel table needs
    its endpoint tables). Any of the three falling back to the live catalog fails this test,
    because the live catalog learns nothing until the commit.
    """
    root = tmp_path / "db"
    with okto_grafx.connect(root, page_size=512) as db:
        with db.begin("write") as txn:
            txn.execute("CREATE VECTOR SPACE s {dimension: 4, metric: 'cosine'}")
            txn.execute("CREATE NODE TABLE P(id INT64, e VECTOR(s), PRIMARY KEY(id))")
            txn.execute("CREATE NODE TABLE Q(id INT64, PRIMARY KEY(id))")
            txn.execute("CREATE REL TABLE K(FROM P TO Q, w INT64)")
            txn.execute("CREATE (:P {id: 1, e: [1.0, 0.0, 0.0, 0.0]})")
            txn.execute("CREATE (:Q {id: 2})")
        with db.begin("write") as txn:
            txn.execute("MATCH (p:P {id: 1}), (q:Q {id: 2}) CREATE (p)-[:K {w: 7}]->(q)")

        assert db.execute("MATCH (p:P)-[:K]->(q:Q) RETURN p.id, q.id").rows == ((1, 2),)
        assert db.verify("all").findings == ()

    with okto_grafx.connect(root, page_size=512) as reopened:
        assert sorted(index.name for index in reopened.indexes.indexes()) == [
            "ef_K",
            "et_K",
            "pk_P",
            "pk_Q",
            "vector_P_s",
        ]
        assert reopened.stale_indexes == ()
        assert reopened.verify("all").findings == ()


def test_two_schema_transactions_in_sequence_allocate_distinct_table_ids(tmp_path: Path) -> None:
    """Each transaction's working copy starts from the pages, so ids come from committed truth.

    A rollback between the two must not burn or duplicate an id: the second transaction reads
    the pages the first one never changed.
    """
    with okto_grafx.connect(tmp_path / "db", page_size=512) as db:
        doomed = db.begin("write")
        doomed.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
        doomed.rollback()
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as txn:
            txn.execute("CREATE NODE TABLE C(id INT64, PRIMARY KEY(id))")
        ids = [t.table_id for t in db.catalog.catalog.tables()]
        assert len(ids) == len(set(ids)) == 2
        assert db.verify("all").findings == ()
