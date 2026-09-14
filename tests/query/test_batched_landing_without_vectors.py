"""A landing table with no vector column reaches the same batched endpoint port (READ-3).

``_closed_vector_free_landings`` declines a target that stores no vector -- its proof has
nothing to withhold -- and BATCH-REL-1 read that verdict as its own admission, so a graph
without vector columns paid one page-0 certificate per landing.  These tests pin the port
counts, prove the batched rows are the scalar rows, and keep the batch out of every shape
the scalar path would have re-proved.

How to read the batched counter.  ``counts["many"]`` is NOT "certificates per landing" and
NOT "batches of 64": ``_OwnerLandingView.landings_many`` reaches the index manager only when
at least one destination of the frontier is still absent from the view's payload memo, so
the counter means **port calls with at least one uncached destination**, and each recorded
key length is the number of uncached DISTINCT destinations that call proved.  The saving is
therefore bounded by the fan-out of ONE source row and is absorbed further by repeated
destinations -- ``test_overlapping_destinations_...`` below measures the near-tie that
follows (100 batched calls against 131 scalar certificates) so the number is never quoted
without its condition.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

import okto_grafx
from okto_grafx.domain.index.definition import RECORD_ID_KEY_DERIVATION
from okto_grafx.engine import query_engine as qe
from okto_grafx.engine.heap_store import HeapStore
from okto_grafx.engine.index_manager import IndexManager, IndexStore
from okto_grafx.errors import GrafxCorruptionDetected

PREFIX = "MATCH (a:A)-[r:E]->(b:B) WHERE a.id='a' "
QUERY = PREFIX + "RETURN b.id, b.title, count(r) ORDER BY b.id"

# One source whose fan-out spans four turns of the frontier refill loop: 64 + 64 + 64 + 8.
WIDE_DESTINATIONS = 200
WIDE_QUERY = PREFIX + "RETURN b.id, b.title ORDER BY b.id"
WIDE_IDS = tuple(f"b{index:03d}" for index in range(WIDE_DESTINATIONS))
# Every third destination is hidden from both landing doors of the mixed-visibility board.
WIDE_HIDDEN = frozenset(WIDE_IDS[::3])

# 100 sources of fan-out 32 over 132 destinations: source ``i`` lands on ``i+1 .. i+32``, so
# consecutive sources share 31 of their 32 destinations.
OVERLAP_SOURCES = 100
OVERLAP_FANOUT = 32
OVERLAP_DESTINATIONS = OVERLAP_SOURCES + OVERLAP_FANOUT
OVERLAP_PREFIX = "MATCH (a:A)-[r:E]->(b:B) WHERE a.id IN $sources "
OVERLAP_PARAMS = {"sources": [f"a{index:03d}" for index in range(OVERLAP_SOURCES)]}


@pytest.fixture
def graph(tmp_path):
    """The vector-free twin of tests/query/test_vector_free_traversal.py's board."""
    with okto_grafx.connect(tmp_path / "db", page_size=8192) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id STRING, title STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM A TO B, rank INT64)")
            tx.execute("CREATE (:A {id:'a'})")
            for i in range(3):
                tx.execute("CREATE (:B {id:$id, title:$title})",
                           {"id": f"b{i}", "title": f"Title {i}"})
                for rank in range(2):
                    tx.execute(
                        "MATCH (a:A {id:'a'}), (b:B {id:$id}) CREATE (a)-[:E {rank:$rank}]->(b)",
                        {"id": f"b{i}", "rank": rank},
                    )
        db.ensure_identity_indexes()
        yield db


@pytest.fixture
def vector_graph(tmp_path):
    """The same board with a vector column on the landing table."""
    with okto_grafx.connect(tmp_path / "db", page_size=8192) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE emb {dimension: 8, metric:'cosine'}")
            tx.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id STRING, title STRING, v VECTOR(emb), "
                       "PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM A TO B, rank INT64)")
            tx.execute("CREATE (:A {id:'a'})")
            for i in range(3):
                tx.execute("CREATE (:B {id:$id, title:$title, v:$v})",
                           {"id": f"b{i}", "title": f"Title {i}", "v": [float(i + 1)] * 8})
                for rank in range(2):
                    tx.execute(
                        "MATCH (a:A {id:'a'}), (b:B {id:$id}) CREATE (a)-[:E {rank:$rank}]->(b)",
                        {"id": f"b{i}", "rank": rank},
                    )
        db.ensure_identity_indexes()
        yield db


@pytest.fixture(scope="module")
def wide_graph(tmp_path_factory):
    """One source with 200 distinct destinations, so the refill loop takes four turns."""
    with okto_grafx.connect(tmp_path_factory.mktemp("read3wide") / "db", page_size=8192) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id STRING, title STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM A TO B, rank INT64)")
            tx.execute("CREATE (:A {id:'a'})")
            for index, identifier in enumerate(WIDE_IDS):
                tx.execute("CREATE (:B {id:$id, title:$title})",
                           {"id": identifier, "title": f"Title {index:03d}"})
        with db.begin("write") as tx:
            for index, identifier in enumerate(WIDE_IDS):
                tx.execute(
                    "MATCH (a:A {id:'a'}), (b:B {id:$id}) CREATE (a)-[:E {rank:$rank}]->(b)",
                    {"id": identifier, "rank": index},
                )
        db.ensure_identity_indexes()
        yield db


@pytest.fixture(scope="module")
def overlapping_graph(tmp_path_factory):
    """100 sources of fan-out 32 whose destination sets overlap in 31 of 32 places."""
    with okto_grafx.connect(tmp_path_factory.mktemp("read3ovl") / "db", page_size=8192) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id STRING, title STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM A TO B, rank INT64)")
            for index in range(OVERLAP_SOURCES):
                tx.execute("CREATE (:A {id:$id})", {"id": f"a{index:03d}"})
            for index in range(OVERLAP_DESTINATIONS):
                tx.execute("CREATE (:B {id:$id, title:$title})",
                           {"id": f"b{index:03d}", "title": f"Title {index:03d}"})
            for source in range(OVERLAP_SOURCES):
                for rank in range(OVERLAP_FANOUT):
                    target = (source + 1 + rank) % OVERLAP_DESTINATIONS
                    tx.execute(
                        "MATCH (a:A {id:$a}), (b:B {id:$b}) CREATE (a)-[:E {rank:$rank}]->(b)",
                        {"a": f"a{source:03d}", "b": f"b{target:03d}", "rank": rank},
                    )
        db.ensure_identity_indexes()
        yield db


def _identity_index(db, table="B"):
    """Return the record-id identity index the endpoint landings of ``table`` resolve through."""
    return next(index for index in db._indexes.active_indexes()
                if index.definition.table_name == table
                and index.definition.key_derivation == RECORD_ID_KEY_DERIVATION)


def _port(monkeypatch):
    """Count the three landing doors of the index-manager port, per operation.

    ``counts["many"]`` counts PORT CALLS WITH AT LEAST ONE UNCACHED DESTINATION, and each
    entry of ``keys`` is how many uncached distinct destinations that one call proved -- never
    "certificates per landing" and never "batches resolved", because ``landings_many`` skips
    the port outright when the view's payload memo already holds every destination of the
    frontier.  ``counts["versions"]`` is the scalar path's one page-0 certificate per distinct
    landing, which is the number the batched counter must be read against.

    The canonical ``__func__`` fences compare the live class attribute against the constant
    query_engine pinned at import, so an observer must re-pin it or it silently turns off the
    path under measurement.
    """
    counts: dict[str, int] = {"many": 0, "landing": 0, "versions": 0}
    keys: list[int] = []
    for label, name, constant in (
        ("many", "validated_identity_landings_many", "_BATCH_LANDING_CANONICAL_MANY"),
        ("landing", "validated_identity_landings", "_BATCH_LANDING_CANONICAL_SCALAR"),
        ("versions", "validated_versions", "_VECTOR_FREE_CANONICAL_VALIDATED"),
    ):
        original = getattr(IndexManager, name)

        def observed(manager, index, key, snapshot, *, _label=label, _original=original):
            counts[_label] += 1
            if _label == "many":
                keys.append(len(key))
            return _original(manager, index, key, snapshot)

        monkeypatch.setattr(IndexManager, name, observed)
        monkeypatch.setattr(qe, constant, observed)
    return counts, keys


def _scalar_oracle(monkeypatch):
    """Force the fallback the batched port replaces, exactly as NODE-IN-SEEK's oracle does."""
    monkeypatch.setattr(IndexManager, "validated_identity_landings_many", None)


def _hide(monkeypatch, db, identifiers, table="B"):
    """Hide chosen destinations from BOTH landing doors, so the two arms stay comparable.

    ``_validated_items`` (scalar) and ``_validated_version_groups`` (batched) differ in which
    heap door they read through -- ``HeapStore.read`` against ``HeapStore.read_landing`` --
    and then ask the very same ``snapshot.visible(xmin, xmax)``.  Pushing ``xmin`` past the
    snapshot behind both doors is therefore ONE injection at the shared decision point, not a
    fake on one side only.  Wrapping ``HeapStore.read`` also has to re-pin the module constant
    query_engine bound at import, or the canonical fence turns the batched path off and the
    measurement measures nothing.

    A committed statement cannot reach this state on its own: ``DELETE`` refuses a node that
    still has relationships and ``DETACH DELETE`` retires the edges in the same commit, so
    under one snapshot an edge and its destination are visible or invisible together.  The
    engine nevertheless has to answer for the mixed case, because the batch admits a landing
    into the frontier before anything validates it.
    """
    table_id = _identity_index(db, table).definition.table_id
    wanted = frozenset(identifiers)
    read, read_landing = HeapStore.read, HeapStore.read_landing
    seen: list[str] = []

    def mask(version):
        if version.table_id != table_id or version.values[0] not in wanted:
            return version
        seen.append(version.values[0])
        return replace(version, xmin=10 ** 12)

    monkeypatch.setattr(HeapStore, "read", lambda heap, ref: mask(read(heap, ref)))
    monkeypatch.setattr(HeapStore, "read_landing",
                        lambda heap, ref: mask(read_landing(heap, ref)))
    monkeypatch.setattr(qe, "_VECTOR_FREE_CANONICAL_READ", HeapStore.read)
    return seen


def test_vector_free_landings_reach_the_batched_port(graph, monkeypatch):
    counts, keys = _port(monkeypatch)
    rows = graph.execute(QUERY).rows
    assert rows == tuple((f"b{i}", f"Title {i}", 2) for i in range(3))
    # One batch for the six steps of the single source; three distinct destinations.
    assert counts["many"] == 1 and keys == [3]
    assert counts["landing"] == 0


def test_scalar_oracle_pays_one_certificate_per_landing(graph, monkeypatch):
    counts, keys = _port(monkeypatch)
    monkeypatch.setattr(qe, "_admits_batched_landings", lambda *_args: False)
    graph.execute(QUERY)
    assert counts["many"] == 0 and keys == []
    assert counts["versions"] == 3  # one page-0 certificate per distinct landing


@pytest.mark.parametrize("suffix", [
    "RETURN b.id, b.title, count(r) ORDER BY b.id",
    "RETURN b.id, r.rank ORDER BY b.id DESC, r.rank ASC LIMIT 3",
    "RETURN count(r)",
    "RETURN b.id, b.title ORDER BY b.title DESC",
    "RETURN DISTINCT b.id ORDER BY b.id",
    "RETURN b ORDER BY b.id",
])
def test_batched_rows_order_columns_and_statistics_equal_the_scalar_path(
    graph, monkeypatch, suffix,
):
    counts, _keys = _port(monkeypatch)
    candidate = graph.execute(PREFIX + suffix)
    assert counts["many"] == 1
    _scalar_oracle(monkeypatch)
    scalar = graph.execute(PREFIX + suffix)
    assert candidate.rows == scalar.rows
    assert candidate.columns == scalar.columns
    assert candidate.statistics == scalar.statistics


@pytest.mark.parametrize("suffix", ["RETURN b.id LIMIT 1", "RETURN b.id",
                                    "RETURN b", "WITH b RETURN count(b)"])
def test_streaming_or_open_consumers_keep_the_scalar_landing(graph, monkeypatch, suffix):
    counts, _keys = _port(monkeypatch)
    graph.execute(PREFIX + suffix)
    assert counts["many"] == 0


def test_vector_landing_without_the_closed_proof_is_never_batched(vector_graph, monkeypatch):
    """The batch withholds vector components; only the closed proof may admit that."""
    monkeypatch.setattr(qe, "_closed_vector_free_landings", lambda _root: frozenset())
    counts, _keys = _port(monkeypatch)
    rows = vector_graph.execute(PREFIX + "RETURN b.id, b.v ORDER BY b.id").rows
    assert counts["many"] == 0
    assert all(len(row[1].values) == 8 for row in rows)


def test_vector_landing_with_the_closed_proof_still_batches(vector_graph, monkeypatch):
    counts, _keys = _port(monkeypatch)
    assert vector_graph.execute(QUERY).rows == tuple(
        (f"b{i}", f"Title {i}", 2) for i in range(3))
    assert counts["many"] == 1


def test_runtime_landing_table_is_reproved_before_any_batch(graph, monkeypatch):
    """A plan-level verdict never admits a batch on a table that stores a vector."""
    original = qe._declares_vector
    monkeypatch.setattr(qe, "_total_landings", lambda engine, node: True)
    monkeypatch.setattr(qe, "_declares_vector",
                        lambda table: True if table.name == "B" else original(table))
    counts, _keys = _port(monkeypatch)
    expected = tuple((f"b{i}", f"Title {i}", 2) for i in range(3))
    assert graph.execute(QUERY).rows == expected
    assert counts["many"] == 0


def test_owner_staged_write_keeps_the_scalar_overlay(graph, monkeypatch):
    counts, _keys = _port(monkeypatch)
    with graph.begin("write") as tx:
        tx.execute("MATCH (a:A),(b:B {id:'b0'}) CREATE (a)-[:E {rank:9}]->(b)")
        assert tx.execute(PREFIX + "RETURN count(r)").rows == ((7,),)
    assert counts["many"] == 0


def test_reader_snapshot_survives_an_independent_writer(graph):
    with graph.begin("read") as reader:
        before = reader.execute(QUERY).rows
        with okto_grafx.connect(graph.path, page_size=8192) as other:
            with other.begin("write") as writer:
                writer.execute("MATCH (b:B {id:'b1'}) SET b.title='new generation'")
        assert reader.execute(QUERY).rows == before
    assert ("b1", "new generation", 2) in graph.execute(QUERY).rows
    assert graph._queries._owner_budget._used_bytes == 0


def test_duplicate_identity_refuses_exactly_as_the_scalar_path(graph, monkeypatch):
    index = next(index for index in graph._indexes.active_indexes()
                 if index.definition.table_name == "B"
                 and index.definition.key_derivation == RECORD_ID_KEY_DERIVATION)
    original = IndexStore._candidates_unchecked

    def duplicates(store, key):
        for entry in original(store, key):
            yield entry
            if store.file == index.file:
                yield entry

    monkeypatch.setattr(IndexStore, "_candidates_unchecked", duplicates)
    with pytest.raises(GrafxCorruptionDetected) as candidate:
        graph.execute(QUERY)
    _scalar_oracle(monkeypatch)
    with pytest.raises(GrafxCorruptionDetected) as scalar:
        graph.execute(QUERY)
    assert candidate.value.to_dict() == scalar.value.to_dict()


def test_invisible_candidates_are_validated_but_never_become_rows(graph, monkeypatch):
    """Both arms read the landing, both refuse it, and neither produces a row for it."""
    counts, _keys = _port(monkeypatch)
    seen = _hide(monkeypatch, graph, ("b0", "b1", "b2"))
    assert graph.execute(QUERY).rows == ()
    assert counts["many"] == 1 and set(seen) == {"b0", "b1", "b2"}
    seen.clear()
    _scalar_oracle(monkeypatch)
    assert graph.execute(QUERY).rows == ()
    assert counts["versions"] == 3 and set(seen) == {"b0", "b1", "b2"}


def test_every_landing_of_a_batch_carries_its_own_certificate(graph, monkeypatch):
    """One probed identity per step, and the rows the scalar path would have produced."""
    original = qe._OwnerLandingView.landings_many
    probed: list[tuple] = []

    def observed(view, identities, context, index):
        answers = original(view, identities, context, index)
        probed.append(tuple(identities))
        assert len(answers) == len(identities)
        return answers

    monkeypatch.setattr(qe._OwnerLandingView, "landings_many", observed)
    rows = graph.execute(QUERY).rows
    # Six steps leave the single source; every one of them is probed, in order.
    assert len(probed) == 1 and len(probed[0]) == 6
    _scalar_oracle(monkeypatch)
    assert rows == graph.execute(QUERY).rows
    assert rows == tuple((f"b{i}", f"Title {i}", 2) for i in range(3))


# --------------------------------------------------------------------------------------
# The frontier refill loop: a fan-out wider than one batch of 64.
# --------------------------------------------------------------------------------------


def test_wide_fan_out_refills_the_frontier_and_returns_the_scalar_rows(
    wide_graph, monkeypatch,
):
    """Four turns of the refill loop, and the rows the scalar path produces, in its order."""
    counts, keys = _port(monkeypatch)
    candidate = wide_graph.execute(WIDE_QUERY)
    # 200 steps leave the single source; the loop admits 64, 64, 64 and then the last 8.
    # Every destination of this board is distinct, so each key length is its frontier size.
    assert keys == [64, 64, 64, 8]
    assert counts["many"] == 4 and counts["landing"] == 0 and counts["versions"] == 0
    _scalar_oracle(monkeypatch)
    scalar = wide_graph.execute(WIDE_QUERY)
    assert counts["many"] == 4  # the oracle reached the port not once more
    assert counts["versions"] == WIDE_DESTINATIONS  # one page-0 certificate per landing
    assert candidate.rows == scalar.rows
    assert candidate.columns == scalar.columns
    assert candidate.statistics == scalar.statistics
    assert candidate.rows == tuple(
        (identifier, f"Title {index:03d}") for index, identifier in enumerate(WIDE_IDS))


@pytest.mark.parametrize("suffix, expected_many", [
    ("RETURN count(r)", 4),
    ("RETURN b ORDER BY b.id", 4),
    ("RETURN b.id, r.rank ORDER BY b.id DESC LIMIT 5", 4),
    ("RETURN DISTINCT b.title ORDER BY b.title", 4),
])
def test_wide_fan_out_shapes_equal_the_scalar_path(
    wide_graph, monkeypatch, suffix, expected_many,
):
    counts, keys = _port(monkeypatch)
    candidate = wide_graph.execute(PREFIX + suffix)
    assert counts["many"] == expected_many and keys == [64, 64, 64, 8]
    _scalar_oracle(monkeypatch)
    scalar = wide_graph.execute(PREFIX + suffix)
    assert counts["versions"] == WIDE_DESTINATIONS
    assert candidate.rows == scalar.rows
    assert candidate.columns == scalar.columns
    assert candidate.statistics == scalar.statistics


def test_a_refusal_on_the_last_refill_turn_matches_the_scalar_refusal(
    wide_graph, monkeypatch,
):
    """A duplicate identity found only on the fourth batch refuses exactly as the scalar path.

    ``_candidate_groups_unchecked`` declines its bucket walk once ``_candidates_unchecked`` is
    not canonical, so both arms visit the same keys in the same frontier order; duplicating
    from the 193rd distinct key on puts the refusal inside the last turn of the refill loop.
    """
    index = _identity_index(wide_graph)
    original = IndexStore._candidates_unchecked
    state = {"seen": 0}

    def duplicates(store, key):
        entries = original(store, key)
        if store.file != index.file:
            return entries
        state["seen"] += 1
        return entries + entries[:1] if state["seen"] > 192 else entries

    monkeypatch.setattr(IndexStore, "_candidates_unchecked", duplicates)
    counts, _keys = _port(monkeypatch)
    with pytest.raises(GrafxCorruptionDetected) as candidate:
        wide_graph.execute(WIDE_QUERY)
    assert counts["many"] == 4  # the refusal was raised from inside the fourth port call
    state["seen"] = 0
    _scalar_oracle(monkeypatch)
    with pytest.raises(GrafxCorruptionDetected) as scalar:
        wide_graph.execute(WIDE_QUERY)
    assert counts["versions"] == 193  # the scalar path stops on the same landing
    assert candidate.value.to_dict() == scalar.value.to_dict()


def test_invisible_destinations_are_batched_validated_and_dropped(wide_graph, monkeypatch):
    """A mixed board: the batch admits invisible destinations, proves them, drops them.

    The frontier is filled from the relationship walk, before anything knows whether a
    destination is visible.  Hiding every third landing keeps all 200 identities in the
    batches -- the key lengths do not move -- while only 133 of them become rows, and those
    are the rows, in the order, the scalar path produces from the same hidden board.
    """
    counts, keys = _port(monkeypatch)
    seen = _hide(monkeypatch, wide_graph, WIDE_HIDDEN)
    candidate = wide_graph.execute(WIDE_QUERY)
    assert keys == [64, 64, 64, 8]  # invisible landings are admitted and proved, not skipped
    assert counts["many"] == 4 and set(seen) == set(WIDE_HIDDEN)
    seen.clear()
    _scalar_oracle(monkeypatch)
    scalar = wide_graph.execute(WIDE_QUERY)
    assert counts["versions"] == WIDE_DESTINATIONS and set(seen) == set(WIDE_HIDDEN)
    assert candidate.rows == scalar.rows
    assert candidate.columns == scalar.columns
    assert candidate.statistics == scalar.statistics
    assert candidate.rows == tuple(
        (identifier, f"Title {index:03d}")
        for index, identifier in enumerate(WIDE_IDS) if identifier not in WIDE_HIDDEN)
    assert len(candidate.rows) == WIDE_DESTINATIONS - len(WIDE_HIDDEN)


# --------------------------------------------------------------------------------------
# Many sources, overlapping destinations: where the batched counter stops paying.
# --------------------------------------------------------------------------------------


def test_overlapping_destinations_batch_once_per_source_not_once_per_64(
    overlapping_graph, monkeypatch,
):
    """The honest counter: 100 port calls against 131 scalar certificates -- a near tie.

    The frontier is born inside ``successors()``, once per source row, so a fan-out of 32
    never fills a batch of 64; and the view's payload memo already absorbed the repeated
    destinations, so the first source proves 32 and each later source proves the single
    destination its window newly reaches.  Nothing here scales with returned rows.
    """
    counts, keys = _port(monkeypatch)
    candidate = overlapping_graph.execute(
        OVERLAP_PREFIX + "RETURN b.id, count(r) ORDER BY b.id", OVERLAP_PARAMS)
    assert counts["many"] == OVERLAP_SOURCES
    assert keys == [OVERLAP_FANOUT] + [1] * (OVERLAP_SOURCES - 1)
    assert sum(keys) == OVERLAP_DESTINATIONS - 1  # b000 is nobody's destination
    _scalar_oracle(monkeypatch)
    scalar = overlapping_graph.execute(
        OVERLAP_PREFIX + "RETURN b.id, count(r) ORDER BY b.id", OVERLAP_PARAMS)
    assert counts["versions"] == OVERLAP_DESTINATIONS - 1
    assert candidate.rows == scalar.rows
    assert candidate.columns == scalar.columns
    assert candidate.statistics == scalar.statistics
    assert len(candidate.rows) == OVERLAP_DESTINATIONS - 1


def test_overlapping_destinations_keep_every_row_and_its_order(
    overlapping_graph, monkeypatch,
):
    """All 3 200 paths, in one total order, identical to the scalar path's."""
    counts, _keys = _port(monkeypatch)
    suffix = "RETURN a.id, b.id, r.rank ORDER BY b.id, a.id, r.rank"
    candidate = overlapping_graph.execute(OVERLAP_PREFIX + suffix, OVERLAP_PARAMS)
    assert counts["many"] == OVERLAP_SOURCES
    _scalar_oracle(monkeypatch)
    scalar = overlapping_graph.execute(OVERLAP_PREFIX + suffix, OVERLAP_PARAMS)
    assert counts["versions"] == OVERLAP_DESTINATIONS - 1
    assert candidate.rows == scalar.rows
    assert candidate.columns == scalar.columns
    assert candidate.statistics == scalar.statistics
    assert len(candidate.rows) == OVERLAP_SOURCES * OVERLAP_FANOUT
