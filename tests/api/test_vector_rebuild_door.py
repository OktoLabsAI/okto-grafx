"""Contract tests for the public vector-index rebuild door.

A stale proximity index is the dangerous kind of stale: it answers from its own entries without
consulting the heap, so it returns a plausible, confidently ordered top-k that silently omits
rows. Nothing repairs one at open. These tests pin the door an operator asks for that repair
through -- what it refuses before touching anything, what it owns while it works, and what it
declines to call ready.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from okto_grafx import Database, connect
from okto_grafx.domain.errors import (
    GrafxError,
    GrafxIndexError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.model.value import VectorValue
from okto_grafx.domain.page.file_header import HEADER_PAGE_INDEX
from okto_grafx.domain.txn.partitions import page_partition
from okto_grafx.engine.public_views import VectorIndexView

PAGE_SIZE: int = 512
SPACE: str = "note_embedding_idx"
SPACE_DDL: str = (
    f"CREATE VECTOR SPACE {SPACE} "
    "{dimension: 4, metric: 'cosine', normalized: false, storage_dtype: 'float64'}"
)
TABLE_DDL: str = (
    f"CREATE NODE TABLE Note(id STRING, embedding VECTOR({SPACE}), PRIMARY KEY(id))"
)
ROWS: int = 3


def _seed(database: Database) -> None:
    """Create the one space, the one table and a few rows carrying real vectors."""
    with database.begin("write") as schema:
        schema.execute(SPACE_DDL)
        schema.execute(TABLE_DDL)
    space_id = database.vectors.index(SPACE).space_id
    with database.begin("write") as rows:
        for ordinal in range(ROWS):
            rows.execute(
                "CREATE (n:Note {id: $id, embedding: $embedding})",
                {
                    "id": f"n{ordinal}",
                    "embedding": VectorValue(
                        values=(1.0, float(ordinal), 0.0, 0.0),
                        space_ref=space_id,
                        dtype="float64",
                    ),
                },
            )


@pytest.fixture()
def seeded(tmp_path: Path) -> Any:
    """Yield an open, writable database whose single vector index is healthy."""
    database = connect(tmp_path / "db", page_size=PAGE_SIZE)
    try:
        _seed(database)
        yield database
    finally:
        if not database.closed:
            database.close()


def _assert_search_refused(database: Database) -> None:
    """Assert the index will not answer a similarity search."""
    space_id = database.vectors.index(SPACE).space_id
    query = VectorValue(
        values=(1.0, 0.0, 0.0, 0.0), space_ref=space_id, dtype="float64"
    )
    with pytest.raises(GrafxIndexError):
        with database.begin("read") as reader:
            reader.execute(
                "MATCH (n:Note) "
                f"WHERE similarity(n.embedding, $query, space => '{SPACE}') > -1.5 "
                "RETURN n.id",
                {"query": query},
            )


def _assert_refused_and_still_refused_cold(database: Database) -> None:
    """Assert the refusal is physical here AND survives the reopen it is deferred to.

    The contract leaves certification of an unproved rebuild to a reopen. That is only safe if
    the refusal itself is durable: an index that came back healthy from cold would hand the
    next process exactly the confident short answer this door declined to give.
    """
    view = database.vectors.index(SPACE)
    assert view.stale is True
    assert view.stale_reason is not None
    _assert_search_refused(database)

    path = database.path
    database.checkpoint()
    database.close()
    with connect(path, page_size=PAGE_SIZE) as reopened:
        cold = reopened.vectors.index(SPACE)
        assert cold.stale is True
        assert cold.stale_reason is not None
        assert cold.name in reopened.stale_indexes
        _assert_search_refused(reopened)


def test_the_door_returns_a_healthy_view_of_the_generation_it_completed(
    seeded: Database,
) -> None:
    """A rebuild that committed reports ready, and the walk agrees with the claim."""
    before = seeded.vectors.index(SPACE)

    view = seeded.maintenance.rebuild_vector_index(SPACE)

    assert isinstance(view, VectorIndexView)
    assert view.name == before.name
    assert view.space_name == SPACE
    assert view.stale is False
    assert view.stale_reason is None
    assert not seeded.verify("all").findings


def test_the_rebuild_claims_only_the_position_its_scan_covered(
    seeded: Database,
) -> None:
    """The claim is the scan, not the commit, and the engine refuses to invent the difference.

    A rebuild reads the heap under its own snapshot and then commits above it, so the position
    it may honestly claim is the one it read at. Clearing through the commit is refused as
    invented coverage, which means a reader that opened above the scan is told the index could
    omit a row -- retryably -- instead of being served a confident short answer. The next commit
    that stages index work carries the claim forward in the ordinary way.
    """
    space_id = seeded.vectors.index(SPACE).space_id
    query = VectorValue(
        values=(1.0, 0.0, 0.0, 0.0), space_ref=space_id, dtype="float64"
    )
    search = (
        "MATCH (n:Note) "
        f"WHERE similarity(n.embedding, $query, space => '{SPACE}') > -1.5 "
        "RETURN n.id"
    )
    scanned_through = seeded.vectors.index(SPACE).built_through_lsn

    view = seeded.maintenance.rebuild_vector_index(SPACE)
    assert view.built_through_lsn == scanned_through

    with pytest.raises(GrafxIndexError) as behind:
        with seeded.begin("read") as reader:
            reader.execute(search, {"query": query})
    assert behind.value.retryable is True

    with seeded.begin("write") as later:
        later.execute(
            "CREATE (n:Note {id: $id, embedding: $embedding})",
            {
                "id": "n9",
                "embedding": VectorValue(
                    values=(1.0, 9.0, 0.0, 0.0), space_ref=space_id, dtype="float64"
                ),
            },
        )

    with seeded.begin("read") as reader:
        result = reader.execute(search, {"query": query})
    assert len(result.rows) == ROWS + 1
    assert not seeded.verify("all").findings


def test_a_repeated_rebuild_is_safe_and_leaves_the_index_healthy(
    seeded: Database,
) -> None:
    """An operator who runs the repair twice gets the same answer, not a broken index."""
    first = seeded.maintenance.rebuild_vector_index(SPACE)
    second = seeded.maintenance.rebuild_vector_index(SPACE)

    assert first.stale is False
    assert second.stale is False
    assert second.name == first.name
    assert not seeded.verify("all").findings


def test_the_door_opens_its_own_transaction_and_names_the_page_it_rewrites(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Durable work that claims no partition could never lose an optimistic validation.

    The page the pass rewrites is the index header that carries the stale mark and the
    built-through position; naming it is what makes two rebuilds of one index conflict rather
    than silently overwrite each other.
    """
    opened: list[str] = []
    captured: list[object] = []
    original = Database.begin

    def spy(self: Database, mode: str = "write") -> Any:
        opened.append(mode)
        transaction = original(self, mode)
        captured.append(transaction)
        return transaction

    monkeypatch.setattr(Database, "begin", spy)
    file = seeded.vectors.index(SPACE).file
    seeded.maintenance.rebuild_vector_index(SPACE)

    assert opened == ["write"]
    context = captured[0]._context  # type: ignore[attr-defined]
    assert page_partition(file, HEADER_PAGE_INDEX) in context.write_partitions


def test_the_door_delegates_to_the_existing_rebuild_generation_semantics(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repair is the engine's own pass, driven from here rather than reimplemented.

    The durable stale generation, the RESET bound to its token and the heap-derived entries are
    what make a concurrent superseding commit refuse instead of overwrite. This door must reach
    that pass with its own transaction and the position its snapshot read at, because a door
    that staged its own records would be a second implementation of the same contract.
    """
    manager = seeded._indexes
    original = type(manager).rebuild
    seen: list[tuple[int, int]] = []

    def spy(self: Any, name: str, txn: Any, through_lsn: int, **claimed: Any) -> int:
        seen.append((txn.txn_id, through_lsn))
        return original(self, name, txn, through_lsn, **claimed)

    monkeypatch.setattr(type(manager), "rebuild", spy)
    view = seeded.maintenance.rebuild_vector_index(SPACE)
    monkeypatch.undo()

    assert len(seen) == 1
    _txn_id, through = seen[0]
    assert through == view.built_through_lsn
    assert view.stale is False


def test_a_close_arriving_in_the_post_commit_window_waits_for_the_door(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard has to cover the window after the transaction stops participating.

    Clearing the stale mark, refreshing the cache and proving the view all happen once the
    commit is done. A close arriving there must not take components apart underneath a door
    that is still using them: the caller is owed either the result or a refusal, never a
    half-dismantled database.
    """
    original = Database._forget_stale_index
    observed: list[bool] = []

    def close_midway(self: Database, name: str) -> None:
        original(self, name)
        self.close()
        observed.append(self.closed)

    monkeypatch.setattr(Database, "_forget_stale_index", close_midway)
    view = seeded.maintenance.rebuild_vector_index(SPACE)
    monkeypatch.undo()

    assert observed == [True]
    assert view.stale is False
    assert view.stale_reason is None
    assert view.built_through_lsn is not None


INTERLEAVINGS: tuple[str, ...] = ("target_row", "vector_ddl", "checkpoint")


@pytest.mark.parametrize("interleaving", INTERLEAVINGS)
def test_work_landing_between_staging_and_the_barrier_never_wedges_the_database(
    seeded: Database, monkeypatch: pytest.MonkeyPatch, interleaving: str
) -> None:
    """A rebuild must lose before the barrier, never leave a redo nobody can complete.

    The claim makes the index durably stale before anything is staged, so work that advances
    the generation in the window that follows is fatal in a specific way: the RESET reaches
    the log durably and then refuses to apply. That is a committed redo with no way forward --
    recovery required, checkpoint refused, and a database that will not reopen. Measured, not
    imagined: without the fence below, a single row written to the target table produced
    exactly that.

    Reading every partition of the target table turns it into an ordinary optimistic refusal
    before the barrier. Only that table is fenced, so unrelated commits still commit.
    """
    space_id = seeded.vectors.index(SPACE).space_id
    manager = seeded._indexes
    original = type(manager).rebuild
    fired: list[str] = []

    def intrude() -> None:
        if interleaving == "target_row":
            with seeded.begin("write") as writer:
                writer.execute(
                    "CREATE (n:Note {id: $id, embedding: $embedding})",
                    {
                        "id": "intruder",
                        "embedding": VectorValue(
                            values=(1.0, 9.0, 0.0, 0.0),
                            space_ref=space_id,
                            dtype="float64",
                        ),
                    },
                )
        elif interleaving == "vector_ddl":
            with seeded.begin("write") as ddl:
                ddl.execute(
                    "CREATE VECTOR SPACE other_embedding_idx "
                    "{dimension: 4, metric: 'cosine', normalized: false, "
                    "storage_dtype: 'float64'}"
                )
        else:
            seeded.checkpoint()

    def stage_then_intrude(
        self: Any, name: str, txn: Any, through_lsn: int, **claimed: Any
    ) -> int:
        staged = original(self, name, txn, through_lsn, **claimed)
        if not fired:
            fired.append(name)
            intrude()
        return staged

    monkeypatch.setattr(type(manager), "rebuild", stage_then_intrude)
    outcome: GrafxError | None = None
    try:
        seeded.maintenance.rebuild_vector_index(SPACE)
    except GrafxError as refused:
        outcome = refused
        details = getattr(refused, "details", {}) or {}
        # Losing is allowed. Losing AFTER the barrier is not.
        assert details.get("durable") is not True
        assert details.get("committed") is not True
    monkeypatch.undo()

    if interleaving == "target_row":
        # This one is not allowed to succeed quietly: without the fence it commits durably and
        # then cannot replay, which is the wedge this test exists for.
        assert outcome is not None
        assert outcome.retryable is True

    assert fired == [seeded.vectors.index(SPACE).name]
    assert seeded.transactions.recovery_required is False
    seeded.checkpoint()
    seeded.close()

    with connect(seeded.path, page_size=PAGE_SIZE) as reopened:
        assert not reopened.verify("all").findings


def test_a_second_rebuild_cannot_finish_inside_the_first_ones_refusal(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retiring the RESET and moving the generation must not be two separately-visible calls.

    Persisting a refusal moves the index's durable generation, and a RESET another rebuild
    committed is replayable only while ITS generation is current. As two public calls that is a
    real window: measured on the previous revision, a rebuild B driven to completion from inside
    A's checkpoint left token 16 against durable generation 20 -- a handle that looked fine hot
    and a database that would not reopen.

    The window is closed by the two halves being one operation, so this test hooks the separate
    checkpoint door: if anything ever reintroduces it, B runs again and the reopen fails.
    """
    from okto_grafx.engine.database import Transaction

    original_commit = Transaction.commit
    original_checkpoint = Database.checkpoint
    state = {"a_committed": False, "b_done": False}
    intruded: list[str] = []

    def commit_then_fail(self: Transaction) -> Any:
        report = original_commit(self)
        if not state["a_committed"]:
            state["a_committed"] = True
            raise GrafxIndexError(
                "injected post-barrier failure", field="injected_a", index="note"
            )
        return report

    def checkpoint_then_let_b_finish(self: Database) -> Any:
        report = original_checkpoint(self)
        if state["a_committed"] and not state["b_done"]:
            state["b_done"] = True
            monkeypatch.setattr(Transaction, "commit", original_commit)
            try:
                intruded.append(self.maintenance.rebuild_vector_index(SPACE).name)
            finally:
                monkeypatch.setattr(Transaction, "commit", commit_then_fail)
        return report

    monkeypatch.setattr(Transaction, "commit", commit_then_fail)
    monkeypatch.setattr(Database, "checkpoint", checkpoint_then_let_b_finish)
    with pytest.raises(GrafxIndexError) as refused:
        seeded.maintenance.rebuild_vector_index(SPACE)
    monkeypatch.undo()

    assert refused.value.details["field"] == "injected_a"
    # The separate checkpoint door is not on this path any more, so B never got its window.
    assert intruded == []
    assert seeded.transactions.recovery_required is False

    # Deliberately NO extra checkpoint here. One taken after the mark would retire whatever
    # the race stranded and hide the very wedge this test exists to catch, so the reopen has
    # to happen on the log exactly as the door left it.
    path = seeded.path
    seeded.close()
    with connect(path, page_size=PAGE_SIZE) as reopened:
        cold = reopened.vectors.index(SPACE)
        assert cold.stale is True
        assert cold.name in reopened.stale_indexes
        _assert_search_refused(reopened)


def test_a_claim_landing_after_staging_refuses_before_the_record_exists(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A RESET whose generation was superseded must never become a durable record.

    The staged reset carries the generation that authorised it. If a claim lands between the
    staging and the append, writing the record anyway produces a redo that refuses to apply --
    on this handle and on every reopen after it. The refusal therefore happens inside the
    commit section, after ordinary validation has already answered, and immediately before the
    records exist.
    """
    manager = seeded._indexes
    original = type(manager).rebuild
    fired: list[str] = []

    def stage_then_claim(
        self: Any, name: str, txn: Any, through_lsn: int, **claimed: Any
    ) -> int:
        staged = original(self, name, txn, through_lsn, **claimed)
        if not fired:
            fired.append(name)
            seeded._transactions.checkpoint_and_claim_index_rebuild(
                self.index(name), "an intruding claim between staging and the barrier"
            )
        return staged

    monkeypatch.setattr(type(manager), "rebuild", stage_then_claim)
    with pytest.raises(GrafxIndexError) as refused:
        seeded.maintenance.rebuild_vector_index(SPACE)
    monkeypatch.undo()

    assert fired != []
    assert refused.value.details["field"] == "rebuild_superseded"
    assert refused.value.details.get("durable") is not True
    assert seeded.transactions.recovery_required is False
    path = seeded.path
    seeded.close()
    with connect(path, page_size=PAGE_SIZE) as reopened:
        assert reopened.vectors.index(SPACE).stale is True
        assert not reopened.verify("all").findings


def test_a_failure_right_after_the_claim_leaves_no_transaction_and_a_stale_index(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim already made the index stale, so everything after it owes the same cleanup.

    Opening the transaction, resolving the table and declaring interest all happen after the
    generation has moved. A failure in any of them must still leave the name in the cache an
    operator reads, no transaction open behind it, and a database that opens.
    """
    original_begin = Database.begin

    def refuse_to_begin(self: Database, mode: str = "write") -> Any:
        raise GrafxIndexError(
            "injected begin failure", field="injected_begin", index="note"
        )

    monkeypatch.setattr(Database, "begin", refuse_to_begin)
    with pytest.raises(GrafxIndexError) as refused:
        seeded.maintenance.rebuild_vector_index(SPACE)
    monkeypatch.setattr(Database, "begin", original_begin)

    assert refused.value.details["field"] == "injected_begin"
    name = seeded.vectors.index(SPACE).name
    assert seeded.vectors.index(SPACE).stale is True
    assert name in seeded.maintenance.status().stale_indexes
    # Nothing was left open: an ordinary transaction still commits.
    with seeded.begin("write") as writer:
        writer.execute("MATCH (n:Note) RETURN n.id")
    assert seeded.transactions.recovery_required is False
    monkeypatch.undo()

    path = seeded.path
    seeded.close()
    with connect(path, page_size=PAGE_SIZE) as reopened:
        assert reopened.vectors.index(SPACE).stale is True
        assert not reopened.verify("all").findings


def test_a_refusal_that_cannot_be_lifted_simply_stays_standing(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing has to republish a refusal that was never lifted.

    The commit deferred the clear, so the claim's own durable mark is the authority for the
    whole window. A finish that fails needs no repair at all -- which is the reason the halves
    were split, and the difference from the shape that tried to recreate the refusal after the
    fact and could fail while doing it.
    """

    def refuse_to_finish(
        self: Any, manager: Any, name: str, position: int, token: int = 0
    ) -> None:
        raise GrafxIndexError(
            "injected finish failure", field="injected_finish", index="note"
        )

    monkeypatch.setattr(
        type(seeded._transactions),
        "checkpoint_and_clear_index_rebuild",
        refuse_to_finish,
    )
    with pytest.raises(GrafxIndexError) as refused:
        seeded.maintenance.rebuild_vector_index(SPACE)
    monkeypatch.undo()

    assert refused.value.details["field"] == "injected_finish"
    _assert_refused_and_still_refused_cold(seeded)


def test_a_lifted_refusal_before_the_proof_is_refused(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The proof is only worth taking while the refusal it relies on is still standing."""
    original = type(seeded.vectors).index
    healthy = seeded.vectors.index(SPACE)
    lifted = VectorIndexView(
        healthy.name,
        healthy.file,
        healthy.space_id,
        healthy.space_name,
        healthy.dimension,
        healthy.metric_of_space,
        healthy.storage_dtype,
        healthy.ef_search,
        False,
        None,
        healthy.built_through_lsn,
        healthy.table_id,
    )
    calls: list[int] = []

    def sometimes_lifted(self: Any, space_name: str, *, table_id=None) -> VectorIndexView:
        calls.append(1)
        if len(calls) > 1:
            return lifted
        return original(self, space_name, table_id=table_id)

    monkeypatch.setattr(type(seeded.vectors), "index", sometimes_lifted)
    with pytest.raises(GrafxIndexError) as refused:
        seeded.maintenance.rebuild_vector_index(SPACE)
    monkeypatch.undo()

    assert refused.value.details["field"] == "rebuild_refusal_lost"


def test_the_door_refuses_to_report_ready_without_the_position_it_completed(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absent built-through position is the absence of proof, not a benign unknown.

    The number is read from page zero and is reported as ``None`` when that page is not
    resident. Returning the view anyway would certify a generation whose receipt this door
    never managed to read, so it refuses instead.
    """
    original = type(seeded.vectors).index
    healthy = seeded.vectors.index(SPACE)
    silent = VectorIndexView(
        healthy.name,
        healthy.file,
        healthy.space_id,
        healthy.space_name,
        healthy.dimension,
        healthy.metric_of_space,
        healthy.storage_dtype,
        healthy.ef_search,
        False,
        None,
        None,
        healthy.table_id,
    )
    calls: list[int] = []

    def sometimes_silent(self: Any, space_name: str, *, table_id=None) -> VectorIndexView:
        calls.append(1)
        if len(calls) > 1:
            return silent
        return original(self, space_name, table_id=table_id)

    monkeypatch.setattr(type(seeded.vectors), "index", sometimes_silent)
    with pytest.raises(GrafxIndexError) as refused:
        seeded.maintenance.rebuild_vector_index(SPACE)
    monkeypatch.undo()

    assert refused.value.details["field"] == "rebuild_position_unproved"
    _assert_refused_and_still_refused_cold(seeded)


def test_the_door_refuses_to_report_ready_when_the_view_still_says_stale(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returning the view is not a formality: it is the proof, and it is checked."""
    original = type(seeded.vectors).index
    healthy = seeded.vectors.index(SPACE)
    lying = VectorIndexView(
        healthy.name,
        healthy.file,
        healthy.space_id,
        healthy.space_name,
        healthy.dimension,
        healthy.metric_of_space,
        healthy.storage_dtype,
        healthy.ef_search,
        True,
        "injected stale verdict",
        healthy.built_through_lsn,
        healthy.table_id,
    )
    calls: list[int] = []

    def sometimes_lying(self: Any, space_name: str, *, table_id=None) -> VectorIndexView:
        calls.append(1)
        if len(calls) > 1:
            return lying
        return original(self, space_name, table_id=table_id)

    monkeypatch.setattr(type(seeded.vectors), "index", sometimes_lying)
    with pytest.raises(GrafxIndexError) as refused:
        seeded.maintenance.rebuild_vector_index(SPACE)
    monkeypatch.undo()

    assert refused.value.details["field"] == "rebuild_incomplete"
    _assert_refused_and_still_refused_cold(seeded)


@pytest.mark.parametrize("preopen", (False, True))
def test_the_finish_absorbs_a_target_write_made_inside_the_deferred_window(
    seeded: Database, monkeypatch: pytest.MonkeyPatch, preopen: bool
) -> None:
    """A commit landing between the deferred commit and the finish must not move page 0.

    The retained RESET is bound to the generation the claim published, and the finish has to
    replay it. A writer that touched the header on the way past -- even one whose own position
    did not move -- shifted that clock and the finish then refused a reset it had to apply. The
    handle that writes matters too: one opened BEFORE the claim still believes the index is
    healthy, so believing is not enough and the device has to be asked.
    """
    path = seeded.path
    space_id = seeded.vectors.index(SPACE).space_id
    early = connect(path, page_size=PAGE_SIZE) if preopen else None
    original = type(seeded._transactions).checkpoint_and_clear_index_rebuild
    intruded: list[str] = []

    def write_then_finish(
        self: Any, manager: Any, name: str, position: int, token: int = 0
    ) -> None:
        if not intruded:
            intruded.append(name)
            writer = early if early is not None else connect(path, page_size=PAGE_SIZE)
            try:
                with writer.begin("write") as inside:
                    inside.execute(
                        "CREATE (n:Note {id: $id, embedding: $embedding})",
                        {
                            "id": "inside-the-window",
                            "embedding": VectorValue(
                                values=(1.0, 5.0, 0.0, 0.0),
                                space_ref=space_id,
                                dtype="float64",
                            ),
                        },
                    )
            finally:
                if early is None:
                    writer.close()
        return original(self, manager, name, position, token)

    monkeypatch.setattr(
        type(seeded._transactions),
        "checkpoint_and_clear_index_rebuild",
        write_then_finish,
    )
    try:
        view = seeded.maintenance.rebuild_vector_index(SPACE)
    finally:
        monkeypatch.undo()
        if early is not None:
            early.close()

    assert intruded != []
    assert view.stale is False
    assert seeded.transactions.recovery_required is False
    assert not seeded.verify("all").findings

    seeded.close()
    with connect(path, page_size=PAGE_SIZE) as reopened:
        assert not reopened.verify("all").findings


def test_a_clear_that_landed_and_then_raised_is_reported_as_the_success_it_is(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A confirmed physical clear must not be reported as a refusal by its own cleanup.

    Everything after the commit point is housekeeping. Turning a housekeeping failure into the
    operation's failure tells a caller the rebuild did not happen and leaves an index that
    answers from it -- the fail-open one door over. The doubt is latched instead.
    """
    manager = seeded._transactions
    original_drop = type(manager)._drop_lease
    original_finish = type(manager).checkpoint_and_clear_index_rebuild
    finishing: list[bool] = []

    def note_finishing(
        self: Any, mgr: Any, name: str, position: int, token: int = 0
    ) -> None:
        finishing.append(True)
        return original_finish(self, mgr, name, position, token)

    def fail_after_the_clear(self: Any, lease: Any) -> Any:
        outcome = original_drop(self, lease)
        if finishing:
            # Only the finish is a commit point. The claim's own checkpoint is ordinary work
            # and must keep failing normally, or this test would prove the wrong thing.
            return RuntimeError("injected lease release failure")
        return outcome

    monkeypatch.setattr(
        type(manager), "checkpoint_and_clear_index_rebuild", note_finishing
    )
    monkeypatch.setattr(type(manager), "_drop_lease", fail_after_the_clear)
    view = seeded.maintenance.rebuild_vector_index(SPACE)
    monkeypatch.undo()

    assert finishing != []

    assert view.stale is False
    assert seeded.vectors.index(SPACE).stale is False


def test_a_rebuild_completed_by_someone_else_is_not_reported_as_this_ones_success(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Names are not identity: a healthy header at the finish belongs to whoever finished it.

    A pauses at its finish, B runs an entire rebuild of the same index, and A resumes. Both
    used the same claim reason -- it is built from the index name -- so the reason cannot tell
    them apart. Only the generation can. Without that check A meets a healthy header, takes the
    fast path and reports B's completion as its own result.
    """
    manager = seeded._transactions
    original = type(manager).checkpoint_and_clear_index_rebuild
    ran_b: list[str] = []

    def let_b_finish_first(
        self: Any, mgr: Any, name: str, position: int, token: int = 0
    ) -> None:
        if not ran_b:
            ran_b.append(name)
            monkeypatch.setattr(
                type(manager), "checkpoint_and_clear_index_rebuild", original
            )
            try:
                seeded.maintenance.rebuild_vector_index(SPACE)
            finally:
                monkeypatch.setattr(
                    type(manager),
                    "checkpoint_and_clear_index_rebuild",
                    let_b_finish_first,
                )
        return original(self, mgr, name, position, token)

    monkeypatch.setattr(
        type(manager), "checkpoint_and_clear_index_rebuild", let_b_finish_first
    )
    with pytest.raises(GrafxIndexError) as refused:
        seeded.maintenance.rebuild_vector_index(SPACE)
    monkeypatch.undo()

    assert ran_b != []
    assert refused.value.details["field"] == "rebuild_superseded"
    assert seeded.transactions.recovery_required is False
    assert not seeded.verify("all").findings


def test_a_failure_leaving_the_section_after_the_clear_is_not_the_doors_failure(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving the participant section is work too, and it happens after the commit point.

    A normal failure there would otherwise escape past a clear that already landed, telling a
    caller the rebuild failed while the index answers from it. The handler therefore sits
    outside that section rather than inside it.
    """
    manager = seeded._transactions
    original_section = type(manager)._participant_section
    original_finish = type(manager).checkpoint_and_clear_index_rebuild
    finishing: list[bool] = []

    def note_finishing(
        self: Any, mgr: Any, name: str, position: int, token: int = 0
    ) -> None:
        finishing.append(True)
        return original_finish(self, mgr, name, position, token)

    @contextmanager
    def failing_exit(self: Any) -> Any:
        with original_section(self):
            yield
        if finishing:
            raise RuntimeError("injected participant section exit failure")

    monkeypatch.setattr(
        type(manager), "checkpoint_and_clear_index_rebuild", note_finishing
    )
    monkeypatch.setattr(type(manager), "_participant_section", failing_exit)
    view = seeded.maintenance.rebuild_vector_index(SPACE)
    monkeypatch.undo()

    assert finishing != []
    assert view.stale is False
    assert seeded.vectors.index(SPACE).stale is False


def test_the_door_refuses_a_space_this_database_does_not_index(
    seeded: Database,
) -> None:
    """Unknown, non-vector and never-attached targets all refuse the same typed way."""
    for wanted in ("no_such_space", "pk_Note"):
        with pytest.raises(GrafxIndexError) as refused:
            seeded.maintenance.rebuild_vector_index(wanted)
        assert refused.value.details["field"] == "space"


def test_the_door_refuses_a_space_that_is_not_named_by_text(seeded: Database) -> None:
    """A name that is not text is refused before any transaction exists to roll back."""
    with pytest.raises(GrafxIndexError) as refused:
        seeded.maintenance.rebuild_vector_index(17)  # type: ignore[arg-type]
    assert refused.value.details["field"] == "space"


def test_a_read_only_handle_refuses_the_repair(tmp_path: Path) -> None:
    """The repair writes, and a read-only handle holds no writer lease to write under."""
    database = connect(tmp_path / "db", page_size=PAGE_SIZE)
    try:
        _seed(database)
        database.checkpoint()
    finally:
        database.close()

    with connect(tmp_path / "db", page_size=PAGE_SIZE, read_only=True) as cold:
        with pytest.raises(GrafxUnsupportedOperation) as refused:
            cold.maintenance.rebuild_vector_index(SPACE)
        assert refused.value.details["field"] == "read_only"


def test_a_closed_handle_refuses_the_repair(tmp_path: Path) -> None:
    """Every door of a closed database refuses, and this one is not an exception."""
    database = connect(tmp_path / "db", page_size=PAGE_SIZE)
    _seed(database)
    database.close()

    with pytest.raises(GrafxUnsupportedOperation):
        database.maintenance.rebuild_vector_index(SPACE)


def test_a_reopened_database_sees_the_rebuilt_index_healthy(tmp_path: Path) -> None:
    """The repair is durable: a cold handle must not have to run it again."""
    database = connect(tmp_path / "db", page_size=PAGE_SIZE)
    try:
        _seed(database)
        database.maintenance.rebuild_vector_index(SPACE)
        database.checkpoint()
    finally:
        database.close()

    with connect(tmp_path / "db", page_size=PAGE_SIZE) as reopened:
        view = reopened.vectors.index(SPACE)
        assert view.stale is False
        assert view.stale_reason is None
        assert view.name not in reopened.stale_indexes
        assert not reopened.verify("all").findings


def test_a_staging_failure_rolls_back_and_leaves_the_index_stale(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-barrier refusal must leave the state that keeps readers honest: stale."""
    manager = seeded._indexes

    def refuse(*_args: object, **_kwargs: object) -> int:
        raise GrafxIndexError(
            "injected staging refusal", field="injected", index="note"
        )

    monkeypatch.setattr(type(manager), "rebuild", refuse)
    with pytest.raises(GrafxIndexError) as refused:
        seeded.maintenance.rebuild_vector_index(SPACE)

    assert refused.value.details["field"] == "injected"
    monkeypatch.undo()

    # The generation is claimed under the commit section BEFORE anything is staged, so a
    # staging refusal leaves the index visibly stale rather than quietly healthy. That is the
    # fail-closed direction: the index stops answering, and the same door repairs it.
    assert seeded.vectors.index(SPACE).stale is True
    assert seeded.vectors.index(SPACE).name in seeded.maintenance.status().stale_indexes
    repaired = seeded.maintenance.rebuild_vector_index(SPACE)
    assert repaired.stale is False
    assert not seeded.verify("all").findings


def test_a_commit_refusal_before_the_barrier_leaves_the_index_stale(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim marks the index stale first, so a lost commit cannot publish a short index."""
    from okto_grafx.engine.database import Transaction

    def refuse(self: Transaction) -> None:
        raise GrafxIndexError(
            "injected pre-barrier commit refusal", field="injected_commit", index="note"
        )

    monkeypatch.setattr(Transaction, "commit", refuse)
    with pytest.raises(GrafxIndexError) as refused:
        seeded.maintenance.rebuild_vector_index(SPACE)
    monkeypatch.undo()

    assert refused.value.details["field"] == "injected_commit"
    stale = seeded.vectors.index(SPACE)
    assert stale.stale is True
    assert stale.stale_reason is not None
    # And the door that failed is also the door that repairs it.
    repaired = seeded.maintenance.rebuild_vector_index(SPACE)
    assert repaired.stale is False
    assert not seeded.verify("all").findings


def test_a_failed_rebuild_shows_up_in_the_cache_an_operator_reads(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Status and the index view must not disagree about which index is stale.

    The pass makes the generation durably stale before it stages anything, so a failure after
    that point leaves an index the view already refuses. An operator reading
    ``maintenance.status()`` has to see the same name, or they will trust an index the engine
    will not answer from.
    """
    from okto_grafx.engine.database import Transaction

    name = seeded.vectors.index(SPACE).name
    assert name not in seeded.maintenance.status().stale_indexes

    def refuse(self: Transaction) -> None:
        raise GrafxIndexError(
            "injected pre-barrier commit refusal", field="injected_cache", index="note"
        )

    monkeypatch.setattr(Transaction, "commit", refuse)
    with pytest.raises(GrafxIndexError):
        seeded.maintenance.rebuild_vector_index(SPACE)
    monkeypatch.undo()

    assert seeded.vectors.index(SPACE).stale is True
    assert name in seeded.maintenance.status().stale_indexes


def test_an_outcome_past_the_barrier_is_settled_but_never_certified_here(
    seeded: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A walk on the handle whose outcome is in doubt is not the cold proof the contract wants.

    The barrier won, so the rebuild is probably durable. This door settles what it can -- it
    recovers and verifies -- and then still refuses: it re-raises the original failure and
    leaves the name visibly stale, so certification belongs to a reopen and to status rather
    than to the process that could not report its own commit.
    """
    from okto_grafx.engine.database import Transaction

    original = Transaction.commit
    name = seeded.vectors.index(SPACE).name
    verified: list[str] = []
    original_verify = Database.verify

    def commit_then_fail(self: Transaction) -> None:
        original(self)
        raise GrafxIndexError(
            "injected post-barrier failure", field="injected_past_barrier", index="note"
        )

    def record_verify(self: Database, scope: str = "all") -> Any:
        verified.append(scope)
        return original_verify(self, scope)

    monkeypatch.setattr(Transaction, "commit", commit_then_fail)
    monkeypatch.setattr(Database, "verify", record_verify)
    with pytest.raises(GrafxIndexError) as refused:
        seeded.maintenance.rebuild_vector_index(SPACE)
    monkeypatch.undo()

    assert refused.value.details["field"] == "injected_past_barrier"
    assert verified == ["all"]
    assert name in seeded.maintenance.status().stale_indexes

    # Status agreeing is not enough: the physical authority has to refuse too, and it has to
    # still refuse from cold, because that reopen is where certification was deferred to.
    _assert_refused_and_still_refused_cold(seeded)
