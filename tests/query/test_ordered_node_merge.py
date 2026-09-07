"""End-to-end contract for the bounded ordered node merge selected by Pulse pages."""

from __future__ import annotations

from pathlib import Path

from okto_grafx import connect
from okto_grafx.domain.model.value import Timestamp


PAGE = (
    "MATCH (n) WHERE n.relevance_score >= $minimum "
    "AND coalesce(n.revocation_reason, '') <> 'source_deleted' "
    "RETURN n.id, label(n), n.created_at "
    "ORDER BY n.created_at DESC, n.id DESC LIMIT $maximum"
)


def _canonical(text: str) -> str:
    """Force the retained scan with a semantic no-op the ordered selector declines."""
    return text.replace(" LIMIT $maximum", " SKIP 0 LIMIT $maximum")


def _seed(database: object) -> None:
    with database.begin("write") as schema:
        for table in ("Decision", "Evidence"):
            schema.execute(
                f"CREATE NODE TABLE {table}("
                "id STRING, created_at TIMESTAMP, relevance_score DOUBLE, "
                "revocation_reason STRING, PRIMARY KEY(id))"
            )
    for ordinal in range(24):
        table = "Decision" if ordinal % 2 == 0 else "Evidence"
        with database.begin("write") as rows:
            rows.execute(
                f"CREATE (:{table} {{"
                "id: $id, created_at: $created_at, relevance_score: $score, "
                "revocation_reason: $reason})",
                {
                    "id": f"n-{ordinal:02d}" if ordinal != 7 else "n-ç",
                    "created_at": Timestamp(1_000 + ordinal // 3),
                    "score": 0.1 if ordinal in (21, 22) else 0.9,
                    "reason": "source_deleted" if ordinal == 23 else None,
                },
            )


def _create_ordered(database: object, *tables: str) -> None:
    for table in tables:
        database.create_index(
            f"by_page_{table.lower()}",
            table,
            ("created_at", "id"),
            layout="ordered",
        )


def test_ordered_merge_matches_the_canonical_filtered_page_with_less_work(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "database", page_size=512) as database:
        _seed(database)
        _create_ordered(database, "Decision", "Evidence")
        parameters = {"minimum": 0.5, "maximum": 5}

        plan = database.explain(PAGE)
        ordered = database.execute(PAGE, parameters)
        canonical = database.execute(_canonical(PAGE), parameters)

        assert any(node.label == "OrderedNodeMerge" for node in plan.walk())
        assert ordered.rows == canonical.rows
        assert ordered.statistics["ordered_merge_rows"] == 5
        assert ordered.statistics["ordered_merge_tables"] == 2
        assert ordered.statistics["rows_scanned"] < canonical.statistics["rows_scanned"]
        assert ordered.statistics["rows_scanned"] <= 10


def test_ordered_merge_pushes_the_exact_pulse_cursor_and_matches_the_scan(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "database", page_size=512) as database:
        _seed(database)
        _create_ordered(database, "Decision", "Evidence")
        text = (
            "MATCH (n) WHERE n.created_at < timestamp($cursor_ts) "
            "OR (n.created_at = timestamp($cursor_ts) AND n.id < $cursor_id) "
            "RETURN n.id, label(n), n.created_at "
            "ORDER BY n.created_at DESC, n.id DESC LIMIT $maximum"
        )
        parameters = {
            "cursor_ts": "1970-01-01T00:00:00.001006Z",
            "cursor_id": "n-20",
            "maximum": 6,
        }

        plan = database.explain(text)
        ordered_node = next(
            node for node in plan.walk() if node.label == "OrderedNodeMerge"
        )
        ordered = database.execute(text, parameters)
        canonical = database.execute(_canonical(text), parameters)

        assert ordered_node.details()["upper_bound"] != "none"
        assert ordered.rows == canonical.rows
        assert ordered.statistics["rows_scanned"] < canonical.statistics["rows_scanned"]


def test_ordered_merge_is_not_selected_until_every_table_has_the_capability(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "database", page_size=512) as database:
        _seed(database)
        _create_ordered(database, "Decision")

        assert all(node.label != "OrderedNodeMerge" for node in database.explain(PAGE).walk())
        assert database.execute(PAGE, {"minimum": 0.5, "maximum": 5}).rows == database.execute(
            _canonical(PAGE), {"minimum": 0.5, "maximum": 5}
        ).rows


def test_potentially_refusing_projection_and_row_timestamp_conversion_keep_the_scan(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "database", page_size=512) as database:
        _seed(database)
        _create_ordered(database, "Decision", "Evidence")
        projection = (
            "MATCH (n) RETURN 10.0 / n.relevance_score "
            "ORDER BY n.created_at DESC, n.id DESC LIMIT 2"
        )
        row_conversion = (
            "MATCH (n) WHERE timestamp(n.id) < timestamp($cursor) RETURN n.id "
            "ORDER BY n.created_at DESC, n.id DESC LIMIT 2"
        )

        assert all(
            node.label != "OrderedNodeMerge" for node in database.explain(projection).walk()
        )
        assert all(
            node.label != "OrderedNodeMerge"
            for node in database.explain(row_conversion).walk()
        )


def test_dirty_owner_keeps_the_scan_overlay_instead_of_the_durable_order(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "database", page_size=512) as database:
        _seed(database)
        _create_ordered(database, "Decision", "Evidence")
        with database.begin("write") as owner:
            owner.execute(
                "CREATE (:Decision {id: 'new-owner', created_at: $created_at, "
                "relevance_score: 1.0})",
                {"created_at": Timestamp(9_000)},
            )
            result = owner.execute(PAGE, {"minimum": 0.5, "maximum": 1})

            assert result.rows[0][0] == "new-owner"
            assert "ordered_merge_rows" not in result.statistics
