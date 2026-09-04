"""The exact regime decodes only the rows it can see and the filter admits (VEC-2).

``HeapStore.read_if`` is the header-first sibling of ``read``: the predicate decides from one
struct unpack of the slot -- record id, xmin, xmax -- and only an accepted version is decoded.
The exact search uses it with visibility first and the caller's filter second, the order the
full read kept. What is held here, by counting the tuple decodes the heap performs: an invisible
row is never decoded, a refused row is never decoded, an invisible row is never shown to the
filter, and the ranking is the one the visible vectors give when scored directly.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxCorruptionDetected
from okto_grafx.domain.ids import RecordRef
from okto_grafx.domain.ports.vectormath import DistanceMetric
from okto_grafx.engine import heap_store as heap_module

from .conftest import SnapshotDouble, VectorFixture

DIMENSION: int = 4
QUERY: tuple[float, ...] = (0.0, 1.0, 2.0, 3.0)


def _values(index: int) -> tuple[float, ...]:
    return tuple(float(index + position) for position in range(DIMENSION))


def _populate(database: VectorFixture, count: int = 12):
    """Return the space, the table and the refs of a small indexed corpus, one commit per row."""
    space = database.create_space("space", DIMENSION)
    table = database.create_table("Chunk", "space")
    refs = [
        database.insert_row(
            table, index + 1, index % 2, space, _values(index), csn=10 + index
        )
        for index in range(count)
    ]
    return space, table, refs


@pytest.fixture
def decodes(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Count every tuple decode the heap performs; a full read of a row performs exactly one."""
    counter = {"count": 0}
    original = heap_module.decode_tuple

    def counting(table, payload):
        counter["count"] += 1
        return original(table, payload)

    monkeypatch.setattr(heap_module, "decode_tuple", counting)
    return counter


class RecordingFilter:
    """A filter that admits even record ids and remembers every id it was asked about."""

    def __init__(self) -> None:
        self.asked: list[int] = []

    @property
    def cardinality(self) -> int:
        return 3

    def admits(self, record_id: int) -> bool:
        self.asked.append(record_id)
        return record_id % 2 == 0


def test_a_row_the_snapshot_cannot_see_is_never_decoded(
    database: VectorFixture, decodes: dict[str, int]
) -> None:
    _populate(database, 12)
    decodes["count"] = 0
    result = database.engine.search(
        space="space", query=QUERY, k=20, snapshot=SnapshotDouble(13)
    )
    assert sorted(hit.record_id for hit in result.hits) == [1, 2, 3, 4]
    assert decodes["count"] == 4
    decodes["count"] = 0
    everything = database.engine.search(
        space="space", query=QUERY, k=20, snapshot=SnapshotDouble(1000)
    )
    assert len(everything.hits) == 12
    assert decodes["count"] == 12


def test_a_row_the_filter_refuses_is_never_decoded_and_an_invisible_row_never_reaches_it(
    database: VectorFixture, decodes: dict[str, int]
) -> None:
    _populate(database, 12)
    decodes["count"] = 0
    recording = RecordingFilter()
    result = database.engine.search(
        space="space",
        query=QUERY,
        k=20,
        snapshot=SnapshotDouble(15),
        candidate_filter=recording,
    )
    assert sorted(hit.record_id for hit in result.hits) == [2, 4, 6]
    assert sorted(recording.asked) == [1, 2, 3, 4, 5, 6]
    assert decodes["count"] == 3


def test_the_ranking_is_the_one_the_visible_vectors_give_when_scored_directly(
    database: VectorFixture,
) -> None:
    space, _table, _refs = _populate(database, 12)
    snapshot = SnapshotDouble(17)
    result = database.engine.search(space="space", query=QUERY, k=5, snapshot=snapshot)
    visible = [
        (index + 1, _values(index))
        for index in range(12)
        if snapshot.visible(10 + index, 0)
    ]
    expected = database.math.top_k(QUERY, visible, 5, space.metric)
    assert [(hit.record_id, hit.score) for hit in result.hits] == expected


def test_a_tombstoned_row_is_not_decoded_after_the_commit_that_ended_it(
    database: VectorFixture, decodes: dict[str, int]
) -> None:
    space, table, refs = _populate(database, 6)
    database.delete_row(table, refs[2], 3, space, _values(2), csn=50)
    decodes["count"] = 0
    before = database.engine.search(
        space="space", query=QUERY, k=10, snapshot=SnapshotDouble(49)
    )
    assert 3 in {hit.record_id for hit in before.hits}
    assert decodes["count"] == 6
    decodes["count"] = 0
    after = database.engine.search(
        space="space", query=QUERY, k=10, snapshot=SnapshotDouble(50)
    )
    assert 3 not in {hit.record_id for hit in after.hits}
    assert decodes["count"] == 5


def test_read_if_decodes_only_an_accepted_version_and_hands_the_header_to_the_predicate(
    database: VectorFixture, decodes: dict[str, int]
) -> None:
    _space, _table, refs = _populate(database, 3)
    full = database.heap.read(refs[1])
    seen: list[tuple[int, int, int]] = []

    def refuse(record_id: int, xmin: int, xmax: int) -> bool:
        seen.append((record_id, xmin, xmax))
        return False

    decodes["count"] = 0
    assert database.heap.read_if(refs[1], refuse) is None
    assert decodes["count"] == 0
    assert seen == [(full.record_id, full.xmin, full.xmax)]
    assert database.heap.read_if(refs[1], lambda *_fields: True) == full
    assert decodes["count"] == 1


def test_read_if_refuses_a_bad_location_exactly_as_read_does(
    database: VectorFixture,
) -> None:
    _populate(database, 1)
    bad = RecordRef(page=0, slot=1)
    with pytest.raises(GrafxCorruptionDetected) as full:
        database.heap.read(bad)
    with pytest.raises(GrafxCorruptionDetected) as header_first:
        database.heap.read_if(bad, lambda *_fields: True)
    assert header_first.value.details == full.value.details


def test_the_metric_of_the_space_still_decides_the_score(
    database: VectorFixture,
) -> None:
    space = database.create_space("dots", DIMENSION, metric=DistanceMetric.DOT)
    table = database.create_table("Dotted", "dots")
    for index in range(4):
        database.insert_row(table, index + 1, 0, space, _values(index), csn=10 + index)
    result = database.engine.search(
        space="dots", query=QUERY, k=4, snapshot=SnapshotDouble(1000)
    )
    expected = database.math.top_k(
        QUERY,
        [(index + 1, _values(index)) for index in range(4)],
        4,
        DistanceMetric.DOT,
    )
    assert [(hit.record_id, hit.score) for hit in result.hits] == expected
