"""The heap META page proves which table chains a replay changed -- by roots, never by guess.

``HeapStore.committed_high_water`` walks a table from ``extent.first_page`` and follows the
``next`` links inside data pages; of the directory entry on heap page 0 it reads only
``first_page``. Allocating a page changes ``last_page``/``page_count`` and leaves the root alone,
so a replayed META image whose roots ``(table_id, first_page)`` equal the device-fresh baseline
cannot have moved any watermark, and one whose roots differ names exactly the tables whose chain
head moved. Anything the directory cannot prove -- a missing or damaged baseline, a duplicate
table, an image of an unexpected type or at an unexpected location -- must fall back to the
complete photograph. FREE pages stay unproved by consensus.

Two families here. The behavioural tests drive the public door and count what the checkpoint
photographs; they FAIL on a tree where META is still an unknown scope and pass once the root
proof lands. The unit tests build synthetic META images from the resident directory and ask the
classifier directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxCorruptionDetected, GrafxError
from okto_grafx.domain.page import PageType
from okto_grafx.engine.buffer_pool import BufferPool
from okto_grafx.engine.commit_redo import CommitRedo
from okto_grafx.engine.heap_store import (
    DIRECTORY_ENTRY_SIZE,
    HEADER_PAGE_INDEX,
    HeapStore,
    TableExtent,
)
from okto_grafx.engine.index_manager import IndexManager

TABLES = ("A", "B", "C")


def _seed(database: object) -> None:
    with database.begin("write") as txn:
        for name in TABLES:
            txn.execute(
                f"CREATE NODE TABLE {name}(id INT64, name STRING, PRIMARY KEY(id))"
            )
    for name in TABLES:
        with database.begin("write") as txn:
            txn.execute(f"CREATE (:{name} {{id: 1, name: 'before'}})")
    database.checkpoint()


def _table_id(database: object, name: str) -> int:
    return database.catalog.catalog.table(name).table_id


def _heap(database: object) -> HeapStore:
    manager = database._transactions._index_manager
    assert type(manager) is IndexManager
    return manager._heap


def _extent(database: object, name: str) -> TableExtent | None:
    return _heap(database)._find_extent(_table_id(database, name))


class _Spy:
    """Every heap walk, attributed to the photograph that asked for it or to the rest."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.inside_photo: list[str] = []
        self.outside_photo: list[str] = []
        self.photos: list[frozenset[int] | None] = []
        self._depth = 0
        real_walk = HeapStore.committed_high_water
        real_photo = IndexManager.table_watermark_photo
        spy = self

        def walk(self_: HeapStore, table: object):  # type: ignore[no-untyped-def]
            (spy.inside_photo if spy._depth else spy.outside_photo).append(table.name)
            return real_walk(self_, table)

        def photo(self_: IndexManager, *args, **kwargs):  # type: ignore[no-untyped-def]
            scope = kwargs.get("refresh_table_ids")
            spy.photos.append(None if scope is None else frozenset(scope))
            spy._depth += 1
            try:
                return real_photo(self_, *args, **kwargs)
            finally:
                spy._depth -= 1

        monkeypatch.setattr(HeapStore, "committed_high_water", walk)
        monkeypatch.setattr(IndexManager, "table_watermark_photo", photo)

    @property
    def proved_scope(self) -> frozenset[int] | None:
        if any(scope is None for scope in self.photos):
            return None
        return frozenset().union(*self.photos)


# --------------------------------------------------------------------------- behavioural


def test_an_allocation_only_meta_change_keeps_the_exact_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rows that force a new page move last_page/page_count, not the root: scope stays {A}."""
    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        before = _extent(database, "A")
        assert before is not None
        with database.begin("write") as txn:
            for position in range(2, 40):
                txn.execute(
                    "CREATE (:A {id: $id, name: $name})",
                    {"id": position, "name": "x" * 1024},
                )
        after = _extent(database, "A")
        assert after is not None
        assert after.page_count > before.page_count, (before, after)
        assert after.first_page == before.first_page, (before, after)
        spy = _Spy(monkeypatch)
        database.checkpoint()
        assert spy.proved_scope == {_table_id(database, "A")}, spy.photos
        assert set(spy.inside_photo) == {"A"}, spy.inside_photo
        assert database.verify("all").findings == ()
    finally:
        database.close()


def test_a_new_root_names_exactly_the_table_that_gained_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A table's first page turns its root from absent to present: the scope is that table."""
    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        with database.begin("write") as txn:
            txn.execute("CREATE NODE TABLE D(id INT64, name STRING, PRIMARY KEY(id))")
        database.checkpoint()  # the catalog change settles under a complete photograph
        assert _extent(database, "D") is None
        with database.begin("write") as txn:
            txn.execute("CREATE (:D {id: 1, name: 'first'})")
        assert _extent(database, "D") is not None
        spy = _Spy(monkeypatch)
        database.checkpoint()
        assert spy.proved_scope == {_table_id(database, "D")}, spy.photos
        assert set(spy.inside_photo) == {"D"}, spy.inside_photo
        assert database.verify("all").findings == ()
    finally:
        database.close()


def _grow_a(database: object) -> None:
    """Allocate at least one new page for A so the checkpoint's replay carries a META image."""
    before = _extent(database, "A")
    assert before is not None
    with database.begin("write") as txn:
        for position in range(2, 40):
            txn.execute(
                "CREATE (:A {id: $id, name: $name})",
                {"id": position, "name": "x" * 1024},
            )
    after = _extent(database, "A")
    assert after is not None and after.page_count > before.page_count, (before, after)


@pytest.mark.parametrize("damage", ("door", "none"))
def test_a_damaged_device_baseline_takes_the_complete_photograph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    """When the device-fresh META cannot be read, no root can be proved: full photograph.

    ``door`` makes the pool's fresh read of heap page 0 fail only while the baseline reader is
    on the stack, so every other page-0 read of the checkpoint keeps working and the damage is
    attributed to the proof alone; ``none`` makes the baseline reader itself answer ``None``,
    the value the implementation gives an unreadable baseline. Either way the checkpoint must
    photograph completely or refuse safely -- and it must photograph: a vacuous pass is not one.
    """
    import inspect

    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        heap_file = _heap(database).file
        _grow_a(database)
        if damage == "door":
            real_fresh = BufferPool.read_fresh_page

            def damaged(self_: BufferPool, file: str, page_index: int):  # type: ignore[no-untyped-def]
                on_baseline_path = any(
                    frame.function == "replay_watermark_meta_baseline"
                    for frame in inspect.stack()
                )
                if (
                    on_baseline_path
                    and file == heap_file
                    and page_index == HEADER_PAGE_INDEX
                ):
                    raise GrafxCorruptionDetected(
                        "synthetic: heap page 0 unreadable", file=file, page=page_index
                    )
                return real_fresh(self_, file, page_index)

            monkeypatch.setattr(BufferPool, "read_fresh_page", damaged)
        else:
            monkeypatch.setattr(
                IndexManager,
                "replay_watermark_meta_baseline",
                lambda self, file, page: None,
            )
        spy = _Spy(monkeypatch)
        refused = False
        try:
            database.checkpoint()
        except GrafxError:
            refused = True  # a safe refusal is acceptable; an unproved shortcut is not
        assert spy.photos or refused, "the checkpoint neither photographed nor refused"
        if spy.photos:
            assert spy.photos[0] is None, spy.photos
            assert set(spy.inside_photo) >= set(TABLES), spy.inside_photo
    finally:
        database.close()


# --------------------------------------------------------------------------------- unit


def _fresh_meta(database: object):  # type: ignore[no-untyped-def]
    heap = _heap(database)
    page = heap._pool.read_fresh_page(heap.file, HEADER_PAGE_INDEX)
    assert page.page_type == int(PageType.META)
    return page


def _directory(page) -> dict[int, tuple[int, TableExtent]]:  # type: ignore[no-untyped-def]
    """Map table_id -> (slot, extent) for every decodable directory entry of a META page."""
    found: dict[int, tuple[int, TableExtent]] = {}
    for slot, view in page.iter_slot_views():
        if page.is_slot_free(slot) or len(view) != DIRECTORY_ENTRY_SIZE:
            continue
        try:
            extent = TableExtent.decode(bytes(view))
        except GrafxError:
            continue
        found[extent.table_id] = (slot, extent)
    return found


def _classify(database: object, page, *, file: str | None = None):  # type: ignore[no-untyped-def]
    """Ask the classifier and normalise its second element to a frozenset of table ids."""
    redo: CommitRedo = database._transactions._commit_redo
    # A fresh baseline map per call: the classifier captures the device-fresh META once per
    # (file, page) key, exactly as one preflight would.
    known, ids = redo._watermark_scope_for_page(
        file or _heap(database).file, page, meta_baselines={}
    )
    if ids is None:
        normalised: frozenset[int] = frozenset()
    elif isinstance(ids, (set, frozenset, tuple, list)):
        normalised = frozenset(ids)
    else:
        normalised = frozenset({ids})
    return bool(known), normalised


def _require_root_proof(database: object) -> None:
    known, ids = _classify(database, _fresh_meta(database))
    if not known:
        pytest.skip("lote 46 ausente: uma imagem META ainda e escopo desconhecido")
    assert ids == frozenset(), ids


def _rewrite(page, slot: int, extent: TableExtent) -> None:  # type: ignore[no-untyped-def]
    page.update_slot(slot, extent.encode())


def _with(extent: TableExtent, **changes: int) -> TableExtent:
    return TableExtent(
        table_id=changes.get("table_id", extent.table_id),
        first_page=changes.get("first_page", extent.first_page),
        last_page=changes.get("last_page", extent.last_page),
        page_count=changes.get("page_count", extent.page_count),
        next_record_id=changes.get("next_record_id", extent.next_record_id),
    )


def test_an_unchanged_meta_image_proves_that_no_root_moved(tmp_path: Path) -> None:
    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        _require_root_proof(database)
        known, ids = _classify(database, _fresh_meta(database))
        assert known and ids == frozenset()
    finally:
        database.close()


def test_moving_only_the_tail_fields_proves_that_no_root_moved(tmp_path: Path) -> None:
    """last_page, page_count and next_record_id are not read by the walk."""
    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        _require_root_proof(database)
        page = _fresh_meta(database)
        slot, extent = _directory(page)[_table_id(database, "A")]
        _rewrite(
            page,
            slot,
            _with(
                extent,
                last_page=extent.last_page,
                page_count=extent.page_count + 3,
                next_record_id=extent.next_record_id + 100,
            ),
        )
        known, ids = _classify(database, page)
        assert known and ids == frozenset(), (known, ids)
    finally:
        database.close()


def test_a_moved_root_names_exactly_its_table(tmp_path: Path) -> None:
    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        _require_root_proof(database)
        page = _fresh_meta(database)
        directory = _directory(page)
        slot_a, extent_a = directory[_table_id(database, "A")]
        _, extent_b = directory[_table_id(database, "B")]
        assert extent_a.first_page != extent_b.first_page
        _rewrite(page, slot_a, _with(extent_a, first_page=extent_b.first_page))
        known, ids = _classify(database, page)
        assert known and ids == {_table_id(database, "A")}, (known, ids)
    finally:
        database.close()


def test_swapped_roots_name_both_tables_and_no_other(tmp_path: Path) -> None:
    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        _require_root_proof(database)
        page = _fresh_meta(database)
        directory = _directory(page)
        slot_a, extent_a = directory[_table_id(database, "A")]
        slot_b, extent_b = directory[_table_id(database, "B")]
        _rewrite(page, slot_a, _with(extent_a, first_page=extent_b.first_page))
        _rewrite(page, slot_b, _with(extent_b, first_page=extent_a.first_page))
        known, ids = _classify(database, page)
        assert known and ids == {_table_id(database, "A"), _table_id(database, "B")}, (
            known,
            ids,
        )
    finally:
        database.close()


def test_a_root_that_disappears_names_the_table_that_lost_it(tmp_path: Path) -> None:
    """An entry re-pointed at an unknown table removes A's root; A must be re-walked."""
    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        _require_root_proof(database)
        page = _fresh_meta(database)
        slot_a, extent_a = _directory(page)[_table_id(database, "A")]
        _rewrite(page, slot_a, _with(extent_a, table_id=extent_a.table_id + 1000))
        try:
            known, ids = _classify(database, page)
        except GrafxError:
            return  # refusing an entry for a table the catalog does not know is also safe
        # Either the proof names A (its root vanished), or it declines to prove at all.
        assert (not known) or (_table_id(database, "A") in ids), (known, ids)
    finally:
        database.close()


def test_a_duplicate_table_id_forces_the_complete_photograph(tmp_path: Path) -> None:
    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        _require_root_proof(database)
        page = _fresh_meta(database)
        directory = _directory(page)
        slot_b, extent_b = directory[_table_id(database, "B")]
        _rewrite(page, slot_b, _with(extent_b, table_id=_table_id(database, "A")))
        try:
            known, _ids = _classify(database, page)
        except GrafxError:
            return  # a safe refusal
        assert not known, "two entries for one table cannot prove any root"
    finally:
        database.close()


def test_a_malformed_directory_entry_forces_the_complete_photograph(
    tmp_path: Path,
) -> None:
    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        _require_root_proof(database)
        page = _fresh_meta(database)
        slot_a, _extent_a = _directory(page)[_table_id(database, "A")]
        try:
            page.update_slot(slot_a, b"\x00" * (DIRECTORY_ENTRY_SIZE - 1))
        except GrafxError:
            pytest.skip(
                "the page refuses a slot of another size; damage cannot be staged here"
            )
        try:
            known, _ids = _classify(database, page)
        except GrafxError:
            return
        assert not known
    finally:
        database.close()


@pytest.mark.parametrize("wrong", ("location", "type"))
def test_an_unexpected_location_or_type_forces_the_complete_photograph(
    tmp_path: Path, wrong: str
) -> None:
    database = connect(str(tmp_path / "db"))
    try:
        _seed(database)
        _require_root_proof(database)
        page = _fresh_meta(database)
        if wrong == "location":
            page.page_index = HEADER_PAGE_INDEX + 5  # a META image that is not page 0
        else:
            page.page_type = int(PageType.FREE)  # FREE stays unproved by consensus
        try:
            known, _ids = _classify(database, page)
        except GrafxError:
            return
        assert not known, wrong
    finally:
        database.close()
