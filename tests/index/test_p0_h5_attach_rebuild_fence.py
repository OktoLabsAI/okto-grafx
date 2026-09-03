"""P0.1 of GRAFX_PERFORMANCE_ROUND_FINAL.md: reproduce or falsify hypothesis H5 on a temporary DB.

H5, as first written, said a handle that ATTACHES to a board another handle created loses its
exact seeks on tables it never wrote after its own first commit -- ``index_view_unavailable``,
every seek becoming a scan, or a retry livelock. ``Index._require_safe_certificate`` refuses
through three sites that share that ``field``: the durable STALE flag, the process-local rebuild
fence (``_completed_rebuild_through``) and header coverage. Those three are told apart here by
MESSAGE and DETAILS, and each arm pins what this commit actually does:

* attach + commits elsewhere (own and foreign): exact seeks stay seeks -- the original H5 is
  FALSIFIED at this base;
* attach + vector rebuild + commits elsewhere: the rebuilt index refuses every snapshot above
  the position its scan read at, retryably, and retries never clear it; nothing in
  ``stale_indexes`` or ``stale`` shows it; only a commit that stages work on that index (or a
  reopen) clears it, while a cold handle answers the same snapshot from the same durable
  entries. That is the mechanism Codex named; it is the design the existing door test
  ``test_the_rebuild_claims_only_the_position_its_scan_covered`` already pins, so nothing here
  relaxes it;
* a durable STALE flag on an exact index is the only path by which a seek becomes a scan, and
  no commit elsewhere heals it;
* the one defect the arms surfaced, and the one change this delivers: a live writer whose own
  commits left log records behind, then a cold open by another participant (whose replay
  advances every proximity header WITHOUT a log record), then this writer's next commit that
  touches that vector index -- met the page-0 sequence fence AFTER the barrier, came back as
  a non-retryable "already committed" refusal and left the handle in ``recovery_required``.
  The commit path now rebases a clean page-0 frame on the same certificate mismatch the read
  path already rebases on; the fence itself is untouched.

Every database lives under ``tmp_path``; nothing here opens a live board.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

import okto_grafx
from okto_grafx import Database, connect
from okto_grafx.domain.errors import GrafxError, GrafxIndexError
from okto_grafx.domain.model.value import VectorValue
from okto_grafx.engine.query_engine import QueryResult

# The package under test must be THIS tree's ``src``, not another checkout's editable install.
_SRC = Path(__file__).resolve().parents[2] / "src"
assert Path(okto_grafx.__file__).resolve().is_relative_to(_SRC), okto_grafx.__file__

PAGE_SIZE: int = 512
SPACE: str = "note_embedding_idx"
SPACE_DDL: str = (
    f"CREATE VECTOR SPACE {SPACE} "
    "{dimension: 4, metric: 'cosine', normalized: false, storage_dtype: 'float64'}"
)
NOTE_DDL: str = (
    f"CREATE NODE TABLE Note(id STRING, embedding VECTOR({SPACE}), PRIMARY KEY(id))"
)
PERSON_DDL: str = "CREATE NODE TABLE Person(id STRING, name STRING, PRIMARY KEY(id))"
COMPANY_DDL: str = "CREATE NODE TABLE Company(id STRING, name STRING, PRIMARY KEY(id))"
ROWS: int = 3
RETRIES: int = 3
FIELD: str = "index_view_unavailable"


def _vector(database: Database, second: float) -> VectorValue:
    space_id = database.vectors.index(SPACE).space_id
    return VectorValue(
        values=(1.0, second, 0.0, 0.0), space_ref=space_id, dtype="float64"
    )


def _seed(database: Database) -> None:
    """Two exact-indexed node tables and one vector-indexed table, a few rows each."""
    with database.begin("write") as schema:
        schema.execute(SPACE_DDL)
        schema.execute(NOTE_DDL)
        schema.execute(PERSON_DDL)
        schema.execute(COMPANY_DDL)
    with database.begin("write") as rows:
        for ordinal in range(ROWS):
            rows.execute(
                "CREATE (p:Person {id: $id, name: $name})",
                {"id": f"p{ordinal}", "name": f"Person {ordinal}"},
            )
            rows.execute(
                "CREATE (c:Company {id: $id, name: $name})",
                {"id": f"c{ordinal}", "name": f"Company {ordinal}"},
            )
            rows.execute(
                "CREATE (n:Note {id: $id, embedding: $embedding})",
                {"id": f"n{ordinal}", "embedding": _vector(database, float(ordinal))},
            )


@pytest.fixture()
def board(tmp_path: Path) -> Iterator[tuple[Path, Database]]:
    """Yield the root and the handle that CREATED the board (the one every attach follows)."""
    root = tmp_path / "board"
    creator = connect(root, page_size=PAGE_SIZE)
    try:
        _seed(creator)
        yield root, creator
    finally:
        if not creator.closed:
            creator.close()


def _seek_company(handle: Database, key: str) -> QueryResult:
    with handle.begin("read") as reader:
        return reader.execute(
            "MATCH (c:Company) WHERE c.id = $id RETURN c.name", {"id": key}
        )


def _create_person(handle: Database, key: str) -> None:
    """A commit on a table none of the indexes under observation cover."""
    with handle.begin("write") as writer:
        writer.execute(
            "CREATE (p:Person {id: $id, name: $name})", {"id": key, "name": key}
        )


def _create_note(handle: Database, key: str) -> None:
    """A commit that stages work on the vector index."""
    with handle.begin("write") as writer:
        writer.execute(
            "CREATE (n:Note {id: $id, embedding: $embedding})",
            {"id": key, "embedding": _vector(handle, 9.0)},
        )


def _search_notes(handle: Database) -> QueryResult:
    with handle.begin("read") as reader:
        return reader.execute(
            "MATCH (n:Note) "
            f"WHERE similarity(n.embedding, $query, space => '{SPACE}') > -1.5 "
            "RETURN n.id",
            {"query": _vector(handle, 0.0)},
        )


def _vector_index(handle: Database) -> Any:
    return handle._indexes.index(handle.vectors.index(SPACE).name)


def _assert_seeked(result: QueryResult) -> None:
    assert len(result.rows) == 1
    assert result.statistics.get("rows_seeked", 0) == 1, dict(result.statistics)
    assert result.statistics.get("rows_scanned", 0) == 0, dict(result.statistics)


def _assert_scanned(result: QueryResult) -> None:
    assert len(result.rows) == 1
    assert result.statistics.get("rows_seeked", 0) == 0, dict(result.statistics)
    assert result.statistics.get("rows_scanned", 0) >= ROWS, dict(result.statistics)


def _refusal(handle: Database) -> GrafxIndexError:
    with pytest.raises(GrafxIndexError) as refused:
        _search_notes(handle)
    return refused.value


# --- arm 1: attach simple -------------------------------------------------------------------


def test_an_attached_handle_keeps_exact_seeks_on_tables_its_commits_never_touch(
    board: tuple[Path, Database],
) -> None:
    """The original H5 does not hold here: seeks on an untouched table stay seeks.

    Own commits elsewhere and a foreign commit elsewhere both advance the database position
    past what ``pk_Company`` was built through; per-table freshness caps the required position
    to that table's high water, so the header still covers it and nothing is refused.
    """
    root, creator = board
    with connect(root, page_size=PAGE_SIZE) as attached:
        assert attached.stale_indexes == ()
        _assert_seeked(_seek_company(attached, "c1"))
        for turn in range(RETRIES):
            _create_person(attached, f"attached-{turn}")
            _assert_seeked(_seek_company(attached, "c1"))
        _create_person(creator, "creator-after-attach")
        _assert_seeked(_seek_company(attached, "c2"))
        assert attached.stale_indexes == ()
        assert attached._indexes.index("pk_Company")._completed_rebuild_through is None
    assert not creator.verify("all").findings


# --- arm 2: the three refusal sites ----------------------------------------------------------


def test_the_three_index_view_unavailable_sites_are_told_apart_by_message_and_details(
    board: tuple[Path, Database],
) -> None:
    """Same ``field`` for all three; the message and the detail keys name the mechanism."""
    root, creator = board
    healthy = creator._indexes.index("pk_Person")

    # (c) header coverage: the durable header is behind the position a snapshot requires.
    certificate = healthy._fresh_certificate()
    behind = certificate.header.built_through_lsn + 1
    with pytest.raises(GrafxIndexError) as coverage:
        healthy._require_safe_certificate(certificate, behind)
    assert coverage.value.details["field"] == FIELD
    assert coverage.value.retryable is True
    assert "covers position" in coverage.value.message
    assert (
        coverage.value.details["built_through_lsn"]
        == certificate.header.built_through_lsn
    )
    assert coverage.value.details["required_lsn"] == behind

    # (b) process-local rebuild fence: this handle rebuilt the index, so it fences every
    # snapshot above the position the scan read at, even with no commit in between.
    view = creator.maintenance.rebuild_vector_index(SPACE)
    fenced = _vector_index(creator)._completed_rebuild_through
    assert fenced == view.built_through_lsn
    with creator.begin("read") as reader:
        above = reader.snapshot.read_lsn
    assert above > fenced
    fence = _refusal(creator)
    assert fence.details["field"] == FIELD
    assert fence.retryable is True
    assert "rebuilt through" in fence.message
    assert fence.details["built_through_lsn"] == fenced
    assert fence.details["required_lsn"] == above

    # (a) durable STALE flag, as a handle that did NOT record the mark meets it on the device.
    # The carried certificate lets the pre-read pass; the post-read of the same lookup sees
    # the foreign flag, retries from the device and refuses at the STALE site.
    with connect(root, page_size=PAGE_SIZE) as attached:
        _assert_seeked(_seek_company(attached, "c0"))
        creator._indexes.index("pk_Company").mark_stale(
            "P0.1: durable flag", persist=True
        )
        with pytest.raises(GrafxIndexError) as stale:
            _seek_company(attached, "c0")
        assert stale.value.details["field"] == FIELD
        assert stale.value.retryable is True
        assert "stale or rebuilding" in stale.value.message
        assert "built_through_lsn" not in stale.value.details
        assert "required_lsn" not in stale.value.details

    messages = {coverage.value.message, fence.message, stale.value.message}
    assert len(messages) == 3


# --- arm 3: attach + rebuild + commit elsewhere ----------------------------------------------


@pytest.mark.parametrize("who_commits_elsewhere", ["attached", "creator"])
def test_a_rebuild_in_the_attached_handle_fences_that_index_until_a_commit_touches_it(
    board: tuple[Path, Database], who_commits_elsewhere: str
) -> None:
    """The mechanism behind Codex's reading of H5, reproduced and bounded.

    After the attached handle rebuilds the vector index, every search above the scanned
    position is refused at the rebuild-fence site. Commits elsewhere -- by this handle or by
    the creator -- move the snapshot further up and change nothing about the fence; retrying
    changes nothing; ``stale_indexes`` and ``stale`` stay silent about it; exact seeks on other
    tables are untouched; a cold handle answers the same snapshot from the same durable index;
    and a commit that stages work on the rebuilt index lifts it. This is the refusal the door
    test already pins, so it is characterised here rather than changed.
    """
    root, creator = board
    with connect(root, page_size=PAGE_SIZE) as attached:
        assert len(_search_notes(attached).rows) == ROWS
        view = attached.maintenance.rebuild_vector_index(SPACE)
        assert view.stale is False
        fenced = _vector_index(attached)._completed_rebuild_through
        assert fenced == view.built_through_lsn

        writer = attached if who_commits_elsewhere == "attached" else creator
        first = _refusal(attached)
        assert first.details["field"] == FIELD
        assert first.retryable is True
        assert "rebuilt through" in first.message
        assert first.details["built_through_lsn"] == fenced
        assert first.details["required_lsn"] > fenced

        for turn in range(RETRIES):
            _create_person(writer, f"elsewhere-{turn}")
            assert _vector_index(attached)._completed_rebuild_through == fenced
            refusals = [_refusal(attached) for _ in range(RETRIES)]
            for refusal in refusals:
                assert refusal.details["field"] == FIELD
                assert refusal.retryable is True
                assert "rebuilt through" in refusal.message
                assert refusal.details["built_through_lsn"] == fenced
                assert refusal.details["required_lsn"] > fenced
            assert len({refusal.message for refusal in refusals}) == 1

        # Invisible to the operator's view of staleness: not stale, just unavailable here.
        assert attached.stale_indexes == ()
        assert attached.vectors.index(SPACE).stale is False
        # Exact seeks elsewhere are untouched: "every seek becomes a scan" does not follow.
        _assert_seeked(_seek_company(attached, "c0"))
        # A cold handle at the same snapshot answers from the same durable entries.
        with connect(root, page_size=PAGE_SIZE) as cold:
            assert len(_search_notes(cold).rows) == ROWS
            assert _vector_index(cold)._completed_rebuild_through is None
        # What lifts it in-handle: a commit that stages work on that index. (With the cold
        # open above having advanced this proximity header, this is also the wedge below.)
        _create_note(attached, "touch")
        assert _vector_index(attached)._completed_rebuild_through is None
        assert len(_search_notes(attached).rows) == ROWS + 1
    assert not creator.verify("all").findings


# --- arm 4: durable STALE on an exact index --------------------------------------------------


def test_a_durably_stale_exact_index_turns_seeks_into_scans_and_no_commit_elsewhere_heals_it(
    board: tuple[Path, Database],
) -> None:
    """The only path from seek to scan is the durable flag, and it is visible and permanent.

    The planner withholds a STALE index, so the keyed read scans and reports ``rows_scanned``
    instead of ``rows_seeked``; a fresh attach lists the index in ``stale_indexes``; commits on
    other tables do not repair it. Retrying is not a repair either.
    """
    root, creator = board
    creator._indexes.index("pk_Company").mark_stale("P0.1: durable flag", persist=True)
    _assert_scanned(_seek_company(creator, "c0"))
    with connect(root, page_size=PAGE_SIZE) as attached:
        assert attached.stale_indexes == ("pk_Company",)
        _assert_scanned(_seek_company(attached, "c0"))
        for turn in range(RETRIES):
            _create_person(attached, f"elsewhere-{turn}")
            _assert_scanned(_seek_company(attached, "c0"))
        _create_person(creator, "creator-elsewhere")
        _assert_scanned(_seek_company(attached, "c0"))
        assert attached.stale_indexes == ("pk_Company",)
        # The other exact index is untouched by its neighbour's mark.
        with attached.begin("read") as reader:
            result = reader.execute(
                "MATCH (p:Person) WHERE p.id = $id RETURN p.name", {"id": "p0"}
            )
        _assert_seeked(result)


# --- the defect: a cold open's unlogged page-0 advance vs a live writer's clean frame --------


@pytest.mark.parametrize("prelude", ["nothing", "searched", "wrote_a_note", "rebuilt"])
def test_a_cold_opens_proximity_header_advance_does_not_wedge_a_live_writer(
    board: tuple[Path, Database], prelude: str
) -> None:
    """A live writer survives a cold open that advanced a proximity header behind its back.

    The writer commits elsewhere (log records past the last checkpoint), a cold handle opens
    and closes (its replay advances the proximity header on page 0 without a log record, so
    the device sequence moves), and the writer then commits work on that vector index. Before
    the rebase in ``_commit_staged`` this met the page-0 sequence fence after the barrier
    (``page_sequence_conflict``, cached < device), surfaced as a non-retryable refusal and
    left the handle in ``recovery_required`` for every later write. No rebuild is needed;
    whatever the handle did with the index before is irrelevant.
    """
    root, creator = board
    with connect(root, page_size=PAGE_SIZE) as writer:
        if prelude == "searched":
            assert len(_search_notes(writer).rows) == ROWS
        elif prelude == "wrote_a_note":
            _create_note(writer, "warm")
        elif prelude == "rebuilt":
            writer.maintenance.rebuild_vector_index(SPACE)
        index = _vector_index(writer)
        before = index._fresh_certificate()

        _create_person(writer, "elsewhere")
        with connect(root, page_size=PAGE_SIZE):
            pass
        advanced = index._fresh_certificate()
        assert advanced.seq > before.seq, (
            "the cold open must have moved page 0 for this arm"
        )
        assert advanced.header.built_through_lsn >= before.header.built_through_lsn

        _create_note(writer, "after-cold-open")
        published = index._fresh_certificate()
        assert published.seq > advanced.seq
        assert published.header.built_through_lsn >= advanced.header.built_through_lsn
        assert not published.header.flags & 0x1  # never marked stale by the commit
        # Not wedged: the next writes, on both kinds of table, go through.
        _create_note(writer, "after-cold-open-2")
        _create_person(writer, "elsewhere-2")
        expected = ROWS + 2 + (1 if prelude == "wrote_a_note" else 0)
        assert len(_search_notes(writer).rows) == expected
    with connect(root, page_size=PAGE_SIZE) as check:
        assert not check.verify("all").findings
        assert len(_search_notes(check).rows) == expected


def test_a_dirty_page_zero_still_meets_the_sequence_fence(
    board: tuple[Path, Database],
) -> None:
    """The rebase only ever drops a CLEAN page-0 frame; the fence keeps refusing a dirty one.

    Dirty page 0 in a live handle while the device moved is the conflict the fence exists for.
    Simulated at the pool: the header frame is pinned and dirtied, then a foreign participant
    advances page 0; the writer's next commit on that index must still be refused by
    ``page_sequence_conflict`` rather than silently overwrite the foreign generation.
    """
    root, creator = board
    from okto_grafx.domain.page.file_header import HEADER_PAGE_INDEX

    writer = connect(root, page_size=PAGE_SIZE)
    try:
        index = _vector_index(writer)
        _create_person(writer, "elsewhere")
        # Dirty this handle's page-0 frame without publishing it.
        with writer._pool.pinned(index.file, HEADER_PAGE_INDEX) as page:
            page.dirty = True
        with connect(root, page_size=PAGE_SIZE):
            pass
        with pytest.raises(GrafxError) as refused:
            _create_note(writer, "after-cold-open")
        assert refused.value.details.get("field") == "page_sequence_conflict"
    finally:
        # The stale dirty frame is refused by the fence at close as well, never written over
        # the foreign generation; the handle still ends closed.
        with pytest.raises(GrafxError) as at_close:
            writer.close()
        assert at_close.value.details.get("field") == "page_sequence_conflict"
        assert writer.closed
    with connect(root, page_size=PAGE_SIZE) as check:
        assert not check.verify("all").findings
