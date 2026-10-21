"""RF05 label-aware ordered pages preserve canonical membership and authority."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.model.value import Timestamp


PAGE = (
    "MATCH (n:Item) RETURN n.id, n.created_at "
    "ORDER BY n.created_at DESC, n.id DESC LIMIT $maximum"
)


def canonical(text):
    return text.replace(" LIMIT $maximum", " SKIP 0 LIMIT $maximum")


def seed(db, count=40, *, ordered=True):
    with db.begin("write") as tx:
        tx.execute(
            "CREATE NODE TABLE Item(id STRING, created_at TIMESTAMP, PRIMARY KEY(id))"
        )
    with db.begin("write") as tx:
        for i in range(count):
            tx.execute(
                "CREATE (:Item {id:$id, created_at:$ts})",
                {"id": f"n-{i:04d}", "ts": Timestamp(1000 + i // 3)},
            )
    if ordered:
        db.create_index("item_page", "Item", ("created_at", "id"), layout="ordered")


def test_label_page_is_bounded_on_1024_nodes(tmp_path):
    with connect(tmp_path / "db", codec="pure") as db:
        seed(db, 1024)
        result = db.execute(PAGE, {"maximum": 16})
        assert result.rows == db.execute(canonical(PAGE), {"maximum": 16}).rows
        assert result.rows == tuple(
            (f"n-{i:04d}", Timestamp(1000 + i // 3)) for i in range(1023, 1007, -1)
        )
        assert result.statistics.get("ordered_candidates_examined") == 16
        assert result.statistics.get("ordered_merge_rows") == 16


def test_membership_rejection_precedes_limit_and_dynamic_label_is_univocal(tmp_path):
    with connect(tmp_path / "db", codec="pure") as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("MATCH(n:Item) WHERE n.id >= 'n-0024' REMOVE n:Item")
            tx.execute("MATCH(n:Item) SET n:Selected:Other")
        for labels in ("Item", "Selected", "Selected:Other"):
            query = PAGE.replace(":Item", ":" + labels)
            result = db.execute(query, {"maximum": 16})
            assert result.rows == db.execute(canonical(query), {"maximum": 16}).rows
            assert len(result.rows) == 16
            assert result.rows[0][0] == "n-0023"
            assert result.statistics.get("ordered_candidates_examined") == 32


def test_membership_and_total_where_both_filter_before_limit(tmp_path):
    with connect(tmp_path / "db", codec="pure") as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("MATCH(n:Item) WHERE n.id >= 'n-0032' REMOVE n:Item")
        query = PAGE.replace(" RETURN", " WHERE n.id < $last RETURN")
        parameters = {"last": "n-0024", "maximum": 16}
        result = db.execute(query, parameters)
        assert result.rows == db.execute(canonical(query), parameters).rows
        assert result.rows[0][0] == "n-0023"
        assert result.statistics.get("ordered_candidates_examined") == 32


@pytest.mark.parametrize("shape", ["entity", "skip", "ascending", "unsafe"])
def test_unproven_shapes_keep_the_canonical_pipeline(tmp_path, shape):
    with connect(tmp_path / "db", codec="pure") as db:
        seed(db)
        query = {
            "entity": PAGE.replace("n.id, n.created_at", "n"),
            "skip": canonical(PAGE),
            "ascending": PAGE.replace("n.id DESC", "n.id ASC"),
            "unsafe": PAGE.replace("n.id, n.created_at", "1 / 0, n.created_at"),
        }[shape]
        assert all(
            node.label != "OrderedNodeMerge" for node in db.explain(query).walk()
        )


def test_keyset_ties_exact_sequence_and_old_snapshot_with_external_writer(tmp_path):
    path = tmp_path / "db"
    with connect(path, codec="pure") as db:
        seed(db)
        bound = PAGE.replace(
            " RETURN",
            " WHERE n.created_at < $ts OR (n.created_at = $ts AND n.id < $pk) RETURN",
        )
        with connect(path, codec="pure") as writer, db.begin("read") as reader:
            expected = reader.execute(canonical(PAGE), {"maximum": 100}).rows
            rows = list(reader.execute(PAGE, {"maximum": 7}).rows)
            with writer.begin("write") as tx:
                tx.execute("MATCH(n:Item {id:'n-0020'}) REMOVE n:Item")
                tx.execute(
                    "CREATE(:Item {id:'new', created_at:$ts})", {"ts": Timestamp(9000)}
                )
            while page := reader.execute(
                bound, {"maximum": 7, "ts": rows[-1][1], "pk": rows[-1][0]}
            ).rows:
                rows.extend(page)
            assert tuple(rows) == expected
        fresh = db.execute(PAGE, {"maximum": 100})
        assert fresh.rows == db.execute(canonical(PAGE), {"maximum": 100}).rows
        assert fresh.rows[0][0] == "new" and "n-0020" not in {r[0] for r in fresh.rows}


def test_owner_changes_and_stale_index_preserve_fallback(tmp_path):
    with connect(tmp_path / "db", codec="pure") as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute("MATCH(n:Item {id:'n-0039'}) REMOVE n:Item")
            result = tx.execute(PAGE, {"maximum": 16})
            assert result.rows == tx.execute(canonical(PAGE), {"maximum": 16}).rows
            assert result.statistics.get("ordered_merge_rows", 0) == 0
        store = next(
            index
            for index in db._indexes._indexes.values()
            if index.definition.name == "item_page"
        )
        store.mark_stale("RF05 deterministic stale control")
        result = db.execute(PAGE, {"maximum": 16})
        assert result.rows == db.execute(canonical(PAGE), {"maximum": 16}).rows
        assert result.statistics.get("ordered_merge_rows", 0) == 0


def test_missing_index_uses_canonical_scan(tmp_path):
    with connect(tmp_path / "db", codec="pure") as db:
        seed(db, ordered=False)
        result = db.execute(PAGE, {"maximum": 16})
        assert result.rows == db.execute(canonical(PAGE), {"maximum": 16}).rows
        assert result.statistics.get("ordered_merge_rows", 0) == 0


def test_null_timestamp_and_null_keyset_keep_canonical_order(tmp_path):
    with connect(tmp_path / "db", codec="pure") as db:
        seed(db, ordered=False)
        with db.begin("write") as tx:
            tx.execute("CREATE(:Item {id:'null-time', created_at:NULL})")
        db.create_index("item_page", "Item", ("created_at", "id"), layout="ordered")
        for maximum in (1, 16, 100):
            assert (
                db.execute(PAGE, {"maximum": maximum}).rows
                == db.execute(canonical(PAGE), {"maximum": maximum}).rows
            )
        query = PAGE.replace(
            " RETURN",
            " WHERE n.created_at < $ts OR (n.created_at = $ts AND n.id < $pk) RETURN",
        )
        parameters = {"maximum": 16, "ts": None, "pk": "n-0020"}
        assert (
            db.execute(query, parameters).rows
            == db.execute(canonical(query), parameters).rows
            == ()
        )


def test_label_with_two_physical_owners_does_not_enter_new_path(tmp_path):
    with connect(tmp_path / "db", codec="pure") as db:
        seed(db)
        with db.begin("write") as tx:
            tx.execute(
                "CREATE NODE TABLE Other(id STRING, created_at TIMESTAMP, PRIMARY KEY(id))"
            )
        with db.begin("write") as tx:
            tx.execute(
                "CREATE(:Other {id:'foreign', created_at:$ts})", {"ts": Timestamp(9000)}
            )
            tx.execute("MATCH(n) SET n:Shared")
        db.create_index("other_page", "Other", ("created_at", "id"), layout="ordered")
        query = PAGE.replace(":Item", ":Shared")
        result = db.execute(query, {"maximum": 16})
        assert result.rows == db.execute(canonical(query), {"maximum": 16}).rows
        assert result.rows[0][0] == "foreign"
        assert result.statistics.get("ordered_merge_rows", 0) == 0
