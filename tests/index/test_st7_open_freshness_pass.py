"""ST-7: one freshness pass per open -- fewer walks and writes, never a blind spot.

Two families. The counting family pins the cost of a clean reopen: the watermark walk is
taken once per photographed regime and a header that already covers its table is not
rewritten. The observation family pins what may never be optimised away: a STALE bit another
participant persists between two passes is seen by the later pass, and an index behind its
table still advances to a durable header.
"""

from __future__ import annotations

import multiprocessing
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

import okto_grafx
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import INDEX_FLAG_STALE, IndexStore

from .conftest import (
    SnapshotDouble,
    TransactionDouble,
    build_database,
    cold_view,
    header_on_device,
)

BORN: int = 10


def _commit_row(database, record_id: int, name: str, csn: int) -> object:
    """Insert a row and commit the index changes it owes, as test_staleness does."""
    txn = TransactionDouble(txn_id=record_id)
    ref = database.insert(record_id, name, csn)
    database.manager.stage_row_insert(
        txn, database.table.table_id, ref, (record_id, name), csn
    )
    database.manager.commit(txn, csn)
    return ref


@pytest.fixture()
def counters(monkeypatch: pytest.MonkeyPatch) -> Counter:
    """Count watermark walks and header advances without changing what either answers."""
    counts: Counter = Counter()
    walk = HeapStore.committed_high_water
    advance = IndexStore.advance_built_through

    def counting_walk(self, table, **kwargs):
        counts["committed_high_water"] += 1
        return walk(self, table, **kwargs)

    def counting_advance(self, lsn):
        counts["advance_built_through"] += 1
        return advance(self, lsn)

    monkeypatch.setattr(HeapStore, "committed_high_water", counting_walk)
    monkeypatch.setattr(IndexStore, "advance_built_through", counting_advance)
    return counts


def _populated(path: Path) -> None:
    handle = okto_grafx.connect(path, page_size=512)
    try:
        with handle.begin("write") as txn:
            txn.execute("CREATE NODE TABLE A(id INT64, x INT64, PRIMARY KEY(id))")
            txn.execute("CREATE NODE TABLE B(id INT64, tag STRING, PRIMARY KEY(id))")
            txn.execute("CREATE REL TABLE E(FROM A TO B, w INT64)")
        with handle.begin("write") as txn:
            txn.execute("CREATE (:A {id: 1, x: 0})")
            txn.execute("CREATE (:B {id: 1, tag: 't1'})")
        with handle.begin("write") as txn:
            txn.execute(
                "MATCH (a:A {id: 1}), (b:B {id: 1}) CREATE (a)-[:E {w: 1}]->(b)"
            )
    finally:
        handle.close()


# --- counting: what a clean reopen pays ---------------------------------------------------------


def test_a_clean_reopen_advances_no_settled_index_header(
    tmp_path: Path, counters: Counter
) -> None:
    # The first reopen after the writing session may legitimately complete lagging headers.
    # By the SECOND reopen every header already covers its table, nothing is replayed, and a
    # mark that rewrites and fsyncs all of them anyway is pure ceremony -- the 161-header cost
    # ST-7 removes. A skip never writes, so it can never invent freshness.
    _populated(tmp_path / "db")
    okto_grafx.connect(tmp_path / "db", page_size=512).close()
    counters.clear()
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    try:
        advances = counters["advance_built_through"]
        assert advances == 0, f"a settled reopen still advanced {advances} headers"
        assert sorted(handle.execute("MATCH (a:A) RETURN a.id").rows) == [(1,)]
    finally:
        handle.close()


def test_a_reopen_with_replay_walks_each_table_watermark_at_most_twice(
    tmp_path: Path, counters: Counter
) -> None:
    # The replay shape is where the three recovery passes all run. Recovery photographs the
    # watermarks once for its own passes -- nothing applies between the two floor checks, and
    # the post-adoption check re-photographs only if redo applied pages -- and the boot-time
    # open, running after the section is released, always photographs its own. More walks
    # than that are the same question asked again of an unchanged heap.
    _populated(tmp_path / "db")
    survivor = okto_grafx.connect(tmp_path / "db", page_size=512)
    with survivor.begin("write") as txn:
        txn.execute("CREATE (:A {id: 2, x: 0})")
    # No close: the WAL suffix stays behind for the next open to replay, the way a crashed
    # writer leaves it. A second participant is always allowed to open the same board.
    counters.clear()
    handle = okto_grafx.connect(tmp_path / "db", page_size=512)
    try:
        walks = counters["committed_high_water"]
        tables = 3  # A, B and E each carry at least one index
        assert walks <= 2 * tables, (
            f"a reopen with replay walked watermarks {walks} times for {tables} tables"
        )
        assert sorted(handle.execute("MATCH (a:A) RETURN a.id").rows) == [(1,), (2,)]
    finally:
        handle.close()
        survivor.close()


# --- observation: what may never be optimised away ----------------------------------------------


def test_a_foreign_stale_persisted_between_passes_is_seen_by_the_later_pass() -> None:
    # assembly.py persists conservative STALE verdicts outside any commit section, so another
    # participant can write one between two of this process's passes. Reusing a certificate
    # across passes would answer from before that write; each pass reads page 0 fresh, and
    # this is the pin that keeps it so.
    database = build_database()
    _commit_row(database, 1, "Ada", BORN)
    first = database.manager.check_replay_floor(BORN, persist_stale=False)
    assert database.exact not in first

    foreign = cold_view(database)
    foreign.exact.mark_stale("a foreign participant refused this index", persist=True)
    assert (
        header_on_device(database.device, database.exact.file).flags & INDEX_FLAG_STALE
    )

    second = database.manager.check_replay_floor(BORN, persist_stale=True)
    assert database.exact in second, (
        "the later pass answered from before the foreign STALE"
    )


def test_an_index_behind_its_table_still_advances_to_a_durable_header() -> None:
    # The D2 filter must never under-advance: an index whose header trails its table's
    # committed state is exactly the one the completion mark exists for, and the claim has to
    # reach the device, not a cache (a lost claim costs a rebuild nobody needed).
    database = build_database()
    database.insert(1, "Ada", BORN)

    database.manager.mark_built_through(BORN + 5)

    assert (
        header_on_device(database.device, database.exact.file).built_through_lsn >= BORN
    )
    other = cold_view(database)
    assert other.manager.open(BORN + 5) == ()


def test_a_header_already_covering_its_table_is_left_alone_by_the_mark() -> None:
    # THE discriminating red-first test for reopening R3-do-plano. Today mark_built_through
    # advances every header to the global position; under ST-7 an index already covering its
    # table's committed state is complete for every question open() can ask -- required_lsn is
    # the TABLE high water -- so rewriting its header to a global number certifies nothing new
    # and costs a flush per index per open. The skip is observable only as an untouched
    # header; a cold participant still accepts the index as fresh.
    database = build_database()
    database.insert(1, "Ada", BORN)
    database.manager.mark_built_through(BORN)  # settle: header covers its table
    settled = header_on_device(database.device, database.exact.file).built_through_lsn

    database.manager.mark_built_through(BORN + 40)

    assert (
        header_on_device(database.device, database.exact.file).built_through_lsn
        == settled
    ), "a complete header was rewritten to a global position it does not need"
    other = cold_view(database)
    assert other.manager.open(BORN + 40) == ()


def test_a_completion_photo_becomes_the_table_local_read_floor() -> None:
    """A skipped global advance and a later global snapshot must agree on the same floor."""
    database = build_database()
    expected = _commit_row(database, 1, "Ada", BORN)
    settled = header_on_device(database.device, database.exact.file).built_through_lsn

    database.manager.mark_built_through(BORN + 40)

    assert (
        header_on_device(database.device, database.exact.file).built_through_lsn
        == settled
    )
    assert database.manager.lookup(
        database.exact.name,
        database.key(1, "Ada"),
        SnapshotDouble(BORN + 40),
    ) == (expected,)


def test_the_mark_still_advances_a_proximity_index_to_the_global_position() -> None:
    # The Pulse regression (msg_179ada60): the ANN freshness contract reads built_through
    # against the GLOBAL replay declaration, not against the table's high water, so a
    # proximity/vector header the mark leaves at its table position reopens as "vector
    # contract not satisfied" for a consumer this repo's suites never see. Proximity-class
    # indexes therefore keep the pre-ST-7 semantics whole: every completion mark advances
    # them to the declared global position, durably.
    database = build_database()
    _commit_row(database, 1, "Ada", BORN)
    database.manager.mark_built_through(
        BORN
    )  # settle both headers at their table floor

    database.manager.mark_built_through(BORN + 40)

    assert (
        header_on_device(database.device, database.proximity.file).built_through_lsn
        == BORN + 40
    ), (
        "a proximity header stayed at its table position; the ANN contract reads the global one"
    )


def test_an_exact_index_keeps_the_table_local_skip_beside_a_vector_neighbour() -> None:
    # The other half of the fix's contract: restoring the global advance for proximity must
    # not quietly hand it back to exact indexes too -- the 161-header cost ST-7 removed is
    # almost entirely exact-class, and open() never asks an exact index for more than its
    # table's high water.
    database = build_database()
    _commit_row(database, 1, "Ada", BORN)
    database.manager.mark_built_through(BORN)
    settled = header_on_device(database.device, database.exact.file).built_through_lsn

    database.manager.mark_built_through(BORN + 40)

    assert (
        header_on_device(database.device, database.exact.file).built_through_lsn
        == settled
    ), "an exact header was rewritten to the global position the skip exists to avoid"
    assert (
        header_on_device(database.device, database.proximity.file).built_through_lsn
        == BORN + 40
    )


def _poison_index_header(root: str, page_size: int, queue) -> None:
    """One spawned foreign participant persisting a conservative STALE bit via page 0.

    This is byte-for-byte what a foreign assembly does when its own open refuses an index
    outside any commit section: decode page 0, set the flag in the header slot, write the page
    back with a correct checksum. No engine internals of the parent are borrowed -- the child
    talks to the FILE, which is the only thing two processes actually share.
    """
    try:
        from okto_grafx.adapters.codec_v1 import PageCodecV1
        from okto_grafx.domain.index.header import INDEX_HEADER_SLOT, IndexHeader
        from okto_grafx.domain.page import HEADER_PAGE_INDEX
        from okto_grafx.engine.index_manager import (
            INDEX_FLAG_STALE as FLAG,
            primary_key_index_name,
        )

        file = Path(root) / "index" / f"{primary_key_index_name('A')}.idx"
        codec = PageCodecV1(page_size)
        raw = file.read_bytes()
        page = codec.decode_page(raw[:page_size], page_index=HEADER_PAGE_INDEX)
        header = IndexHeader.decode(page.read_slot(INDEX_HEADER_SLOT))
        poisoned = replace(header, flags=header.flags | FLAG)
        page.update_slot(INDEX_HEADER_SLOT, poisoned.encode())
        encoded = codec.encode_page(page)
        with open(file, "r+b") as handle:
            handle.seek(0)
            handle.write(encoded)
        queue.put(("ok", str(file)))
    except BaseException as failure:  # pragma: no cover - transported to the parent
        queue.put(("error", repr(failure)))


def test_a_stale_persisted_by_a_spawned_process_is_observed_at_the_next_open(
    tmp_path: Path,
) -> None:
    # The real two-process shape behind the cold_view pin above: a foreign PARTICIPANT
    # persists the conservative verdict between this process's opens, and the next open must
    # read it off page 0 -- reported through stale_indexes, with the query falling back to the
    # scan rather than answering from a refused index. Any freshness pass that reuses a
    # certificate across that boundary would answer from before the foreign write.
    root = tmp_path / "db"
    _populated(root)
    context = multiprocessing.get_context("spawn")
    queue = context.SimpleQueue()
    child = context.Process(target=_poison_index_header, args=(str(root), 512, queue))
    child.start()
    verdict, detail = queue.get()
    child.join(timeout=60)
    assert verdict == "ok", detail

    handle = okto_grafx.connect(root, page_size=512)
    try:
        from okto_grafx.engine.index_manager import primary_key_index_name

        assert primary_key_index_name("A") in handle.stale_indexes, (
            "the open answered from before the foreign STALE"
        )
        rows = handle.execute("MATCH (a:A) WHERE a.id = 1 RETURN a.id").rows
        assert sorted(rows) == [(1,)], "the fallback scan still owes the correct answer"
    finally:
        handle.close()


def test_a_stale_index_never_moves_its_claim_under_the_mark() -> None:
    # Unchanged semantics, repinned next to the new filter: staleness is sticky and only a
    # rebuild clears it, so the mark must neither advance nor unflag a stale index -- with or
    # without the D2 skip in front of it.
    database = build_database()
    database.insert(1, "Ada", BORN)
    database.exact.mark_stale("behind a replay floor", persist=True)
    before = header_on_device(database.device, database.exact.file)

    database.manager.mark_built_through(BORN + 40)

    after = header_on_device(database.device, database.exact.file)
    assert after.flags & INDEX_FLAG_STALE
    assert after.built_through_lsn == before.built_through_lsn
