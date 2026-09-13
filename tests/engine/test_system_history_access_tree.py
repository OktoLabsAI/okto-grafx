"""Independent ordered-map and corruption evidence for the temporal access tree."""

import random

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.page import Page
from okto_grafx.engine.system_history_index import HistoryAccessTree


def test_tree_randomized_ranges_predecessors_and_reopen():
    pages = {}
    def read(_, number):
        return pages[number]
    tree = HistoryAccessTree(read, database_uuid=b"a" * 16, page_size=512, sequence=7)
    keys = [(table, rid, csn) for table in (1, 2) for rid in range(12) for csn in (1, 3, 7)]
    random.Random(17).shuffle(keys)
    expected = {}
    for key in keys:
        expected[key] = repr(key).encode() * 80
        tree.insert(key, expected[key])
    pages.update(tree.images)
    reader = HistoryAccessTree(read, database_uuid=b"a" * 16, page_size=512, sequence=7, root=tree.root)
    assert [(key, reader.value(ref)) for key, ref in reader.entries((0, 0, 0), (3, 0, 0))] == sorted(expected.items())
    for rid in range(13):
        for csn in range(9):
            coordinate = (1, rid, csn)
            candidates = [key for key in expected if key <= coordinate]
            result = reader.floor(coordinate)
            assert (None if result is None else result[0]) == (max(candidates) if candidates else None)
    assert [key for key, _ in reader.entries((2, 5, 0), (2, 5, 8))] == [(2, 5, csn) for csn in (1, 3, 7)]
    before = tree.root
    next_tree = HistoryAccessTree(read, database_uuid=b"a" * 16, page_size=512, sequence=9,
                                  root=before, first_page=tree.next_page)
    next_tree.insert((1, 5, 9), b"new")
    pages.update(next_tree.images)
    assert reader.floor((1, 5, 10))[0] == (1, 5, 7)
    assert next_tree.floor((1, 5, 10))[0] == (1, 5, 9)
    assert len(next_tree.images) < 12


def test_tree_crc_valid_substitution_and_foreign_identity_refused():
    tree = HistoryAccessTree(lambda *_: b"", database_uuid=b"a" * 16, page_size=512, sequence=7)
    tree.insert((1, 1, 7), b"value")
    pages = dict(tree.images)
    reader = HistoryAccessTree(lambda _, number: pages[number], database_uuid=b"b" * 16,
                               page_size=512, sequence=7, root=tree.root)
    with pytest.raises(GrafxCorruptionDetected):
        reader.floor((1, 1, 7))
    page = Page.from_bytes(pages[tree.root[0]])
    raw = bytearray(page.read_slot(0))
    raw[-1] ^= 1
    page.update_slot(0, bytes(raw))
    pages[tree.root[0]] = page.to_bytes()
    reader.database_uuid = b"a" * 16
    with pytest.raises(GrafxCorruptionDetected):
        reader.floor((1, 1, 7))


def test_qualified_extent_refuses_even_a_valid_unused_tail_page():
    tree = HistoryAccessTree(lambda *_: b"", database_uuid=b"a" * 16, page_size=512, sequence=7)
    tree.insert((1, 1, 7), b"value")
    def forbidden(*_):
        pytest.fail("unreferenced physical tail was read")
    reader = HistoryAccessTree(forbidden, database_uuid=b"a" * 16, page_size=512,
        sequence=7, root=tree.root, read_extent=tree.root[0])
    with pytest.raises(GrafxCorruptionDetected):
        reader.floor((1, 1, 7))
