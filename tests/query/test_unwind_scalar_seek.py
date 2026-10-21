"""Scalar UNWIND seeks preserve binding identity and per-occurrence semantics."""

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxIndexError
from okto_grafx.engine import query_engine as qe
from okto_grafx.engine.index_manager import HashIndex


@pytest.fixture
def database(tmp_path):
    with okto_grafx.connect(tmp_path / "db", page_size=512) as db:
        with db.begin("write") as tx:
            tx.execute(
                "CREATE NODE TABLE Item(id STRING, value INT64, PRIMARY KEY(id))"
            )
        with db.begin("write") as tx:
            tx.execute(
                "UNWIND $rows AS r CREATE (:Item {id:r.id,value:r.value})",
                {
                    "rows": [
                        {"id": "a", "value": 1},
                        {"id": "b", "value": 2},
                        {"id": "雪", "value": 3},
                    ]
                },
            )
        yield db


def labels(db, query):
    return tuple(node.label for node in db.explain(query).walk())


@pytest.mark.parametrize(
    "prefix,key",
    [
        ("UNWIND $ids AS i", "i"),
        ("UNWIND $ids AS i WITH i", "i"),
        ("UNWIND $ids AS i WITH i AS k", "k"),
    ],
)
def test_scalar_binding_drives_pk_seek(database, prefix, key):
    query = prefix + f" MATCH (n:Item {{id:{key}}}) RETURN n.id"
    assert "IndexSeek" in labels(database, query)
    assert "NodeScan" not in labels(database, query)
    result = database.execute(query, {"ids": ["b", "a", "b", "missing", "雪", None]})
    assert result.rows == (("b",), ("a",), ("b",), ("雪",))


@pytest.mark.parametrize(
    "prefix,key",
    [
        ("UNWIND $ids AS i WITH $replacement AS i", "i"),
        ("UNWIND $ids AS i WITH toString(i) AS i", "i"),
        ("UNWIND $ids AS i WITH count(i) AS i", "i"),
    ],
)
def test_rebinding_does_not_inherit_unwind_authority(database, prefix, key):
    query = prefix + f" MATCH (n:Item {{id:{key}}}) RETURN n.id"
    assert "IndexSeek" not in labels(database, query)


@pytest.mark.parametrize("ids", [[], [None], [True, 1, 1.0], ["missing"]])
def test_empty_null_missing_and_incompatible_keys(database, ids):
    result = database.execute(
        "UNWIND $ids AS i MATCH (n:Item {id:i}) RETURN n.id", {"ids": ids}
    )
    assert result.rows == ()


def test_rebound_value_is_used_not_old_input(database):
    result = database.execute(
        "UNWIND $ids AS i WITH $replacement AS i MATCH (n:Item {id:i}) RETURN n.id",
        {"ids": ["a", "a"], "replacement": "b"},
    )
    assert result.rows == (("b",), ("b",))


def test_duplicate_occurrences_keep_write_effects(database):
    assert "IndexSeek" not in labels(
        database,
        "UNWIND $ids AS i MATCH (n:Item {id:i}) SET n.value=n.value+1 RETURN n.value",
    )
    with database.begin("write") as tx:
        result = tx.execute(
            "UNWIND $ids AS i MATCH (n:Item {id:i}) SET n.value=n.value+1 RETURN n.value",
            {"ids": ["a", "a", "missing"]},
        )
        # The canonical MATCH read phase observes value=1 for both occurrences.
        assert result.rows == ((2,), (2,))
    assert database.execute("MATCH (n:Item {id:'a'}) RETURN n.value").rows == ((2,),)


def test_own_created_key_remains_visible(database):
    with database.begin("write") as tx:
        tx.execute("CREATE (:Item {id:'own',value:4})")
        result = tx.execute(
            "UNWIND $ids AS i MATCH (n:Item {id:i}) RETURN n.id,n.value",
            {"ids": ["own", "a", "own"]},
        )
        assert result.rows == (("own", 4), ("a", 1), ("own", 4))


def test_optional_preserves_unmatched_occurrences(database):
    result = database.execute(
        "UNWIND $ids AS i OPTIONAL MATCH (n:Item {id:i}) RETURN i,n.id",
        {"ids": ["a", "missing", None, "a"]},
    )
    assert result.rows == (("a", "a"), ("missing", None), (None, None), ("a", "a"))


def test_second_unwind_and_renaming_do_not_capture_old_value(database):
    result = database.execute(
        "UNWIND ['a'] AS i WITH i AS old UNWIND ['b','雪'] AS i "
        "MATCH (n:Item {id:i}) RETURN old,n.id"
    )
    assert result.rows == (("a", "b"), ("a", "雪"))


def test_old_snapshot_and_new_snapshot_see_their_own_versions(database):
    with database.begin("read") as old:
        assert old.execute("MATCH (n:Item {id:'a'}) RETURN n.value").rows == ((1,),)
        with database.begin("write") as writer:
            writer.execute("MATCH (n:Item {id:'a'}) SET n.value=9")
        query = "UNWIND ['a','a'] AS i MATCH (n:Item {id:i}) RETURN n.value"
        assert old.execute(query).rows == ((1,), (1,))
        assert database.execute(query).rows == ((9,), (9,))


@pytest.mark.parametrize("key", [-(2**63), 2**63 - 1, 1, 1.0, True, False, None, "1"])
def test_numeric_equality_matches_canonical_scan(tmp_path, key):
    with okto_grafx.connect(tmp_path / "numeric") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Number(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute(
                "UNWIND $ids AS i CREATE (:Number {id:i})",
                {"ids": [-(2**63), 1, 2**63 - 1]},
            )
        candidate = "UNWIND $ids AS i MATCH (n:Number {id:i}) RETURN n.id"
        canonical = (
            "UNWIND $ids AS i MATCH (n:Number) WHERE n.id=i OR false RETURN n.id"
        )
        parameters = {"ids": [key, key]}
        assert "IndexSeek" in labels(db, candidate)
        assert "IndexSeek" not in labels(db, canonical)
        assert (
            db.execute(candidate, parameters).rows
            == db.execute(canonical, parameters).rows
        )


@pytest.mark.parametrize("distinct", [8, 16])
def test_1024_nodes_16_occurrences_use_16_probes(tmp_path, distinct):
    with okto_grafx.connect(tmp_path / "scale") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Item(id STRING, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            tx.execute(
                "UNWIND $ids AS i CREATE (:Item {id:i})",
                {"ids": [f"k{i}" for i in range(1024)]},
            )
        ids = [f"k{i % distinct}" for i in range(16)]
        result = db.execute(
            "UNWIND $ids AS i MATCH (n:Item {id:i}) RETURN n.id", {"ids": ids}
        )
        assert result.rows == tuple((key,) for key in ids)
        assert result.statistics.get("rows_seeked") == 16
        assert result.statistics.get("rows_scanned", 0) == 0
        # The existing certified memo saves physical work, never occurrences.
        assert result.statistics.get("index_probe_keys") == distinct
        assert result.statistics.get("physical_node_versions_examined") == distinct


@pytest.mark.parametrize("missing", [False, True])
def test_stale_or_absent_authority_keeps_existing_seek_contract(
    database, monkeypatch, missing
):
    original = qe._IndexAuthorityProjection.named

    class Stale:
        stale = True

        def __init__(self, store):
            self.store = store

        def __getattr__(self, name):
            return getattr(self.store, name)

    def named(self, name):
        store = original(self, name)
        return None if missing or store is None else Stale(store)

    monkeypatch.setattr(qe._IndexAuthorityProjection, "named", named)
    query = "UNWIND ['a','b','a'] AS i MATCH (n:Item {id:i}) RETURN n.id"
    if missing:
        # A store removed from an already captured ACTIVE projection is an
        # authority violation, not an absent optional index. Preserve refusal.
        with pytest.raises(GrafxIndexError) as scalar:
            database.execute(query)
        with pytest.raises(GrafxIndexError) as existing:
            database.execute(
                "UNWIND [{id:'a'}] AS r MATCH (n:Item {id:r.id}) RETURN n.id"
            )
        assert scalar.value.code == existing.value.code
        assert scalar.value.details == existing.value.details
        return
    result = database.execute(query)
    assert result.rows == (("a",), ("b",), ("a",))


def test_generation_retry_after_first_key_never_replays_prefix(database, monkeypatch):
    original = HashIndex.finish_exact_read
    calls = []

    def reject_second(store, before, lsn):
        stable = original(store, before, lsn)
        calls.append(1)
        return False if len(calls) == 2 else stable

    monkeypatch.setattr(HashIndex, "finish_exact_read", reject_second)
    result = database.execute(
        "UNWIND ['a','b','雪'] AS i MATCH (n:Item {id:i}) RETURN n.id"
    )
    assert result.rows == (("a",), ("b",), ("雪",))
    assert result.statistics.get("certificate_retries") == 1


def test_scalar_extension_does_not_admit_non_pk_index(database):
    with database.begin("write") as tx:
        tx.execute("CREATE INDEX by_value FOR (n:Item) ON (n.value)")
    query = "UNWIND [1,2] AS i MATCH (n:Item {value:i}) RETURN n.id"
    assert "IndexSeek" not in labels(database, query)
    assert database.execute(query).rows == (("a",), ("b",))


def test_undemanded_tail_and_first_error_keep_streaming_order(database):
    assert database.execute(
        "UNWIND ['a','b'] AS i MATCH (n:Item {id:i}) RETURN 1/(n.value-2) LIMIT 1"
    ).rows == ((-1.0,),)
    assert (
        database.execute(
            "UNWIND ['a','b'] AS i MATCH (n:Item {id:i}) RETURN 1/(n.value-1) LIMIT 0"
        ).rows
        == ()
    )
