"""Public creation and execution contract for catalog-v2 custom exact indexes."""

from __future__ import annotations

from pathlib import Path

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxIndexError,
    GrafxUnsupportedOperation,
)
from okto_grafx.domain.index.visibility import IndexVisibility
from okto_grafx.domain.query.plan import IndexSeek, plan_nodes


def _seed_people(database: object) -> None:
    with database.begin("write") as schema:
        schema.execute(
            "CREATE NODE TABLE Person("
            "id INT64, email STRING, score INT64, PRIMARY KEY(id))"
        )
    with database.begin("write") as rows:
        rows.execute("CREATE (:Person {id: 1, email: 'ada@example.test', score: 7})")
        rows.execute("CREATE (:Person {id: 2, email: 'grace@example.test', score: 8})")
        rows.execute("CREATE (:Person {id: 3})")


def test_python_door_builds_queries_and_cold_reopens_a_custom_exact_index(
    tmp_path: Path,
) -> None:
    root = tmp_path / "database"
    with connect(root, page_size=512) as database:
        _seed_people(database)

        created = database.create_index(
            "by_email",
            "Person",
            ("email",),
            expected_cardinality=1_000,
        )

        assert created.name == "by_email"
        assert created.table_name == "Person"
        assert created.columns == ("email",)
        assert created.positions == (1,)
        assert created.visibility is IndexVisibility.EXACT
        assert created.key_derivation == "columns"
        assert created.automatic is False
        assert created.generation_state == "active"
        assert created.active_nonce is not None
        assert created.expected_cardinality == 1_000
        assert created.bucket_count == 16
        assert created.built_through_lsn is not None
        assert created.reconciled_through_lsn is not None
        assert "by_email" in database.attached_indexes

        plan = database.explain(
            "MATCH (p:Person) WHERE p.email = 'grace@example.test' RETURN p.id"
        )
        assert any(isinstance(node, IndexSeek) for node in plan_nodes(plan))
        assert database.execute(
            "MATCH (p:Person) WHERE p.email = 'grace@example.test' RETURN p.id"
        ).rows == ((2,),)
        assert database.verify("all").findings == ()
        nonce = created.active_nonce
        file = created.file

    with connect(root, page_size=512) as reopened:
        restored = reopened.indexes.index("by_email")
        assert restored.active_nonce == nonce
        assert restored.file == file
        assert restored.columns == ("email",)
        assert reopened.execute(
            "MATCH (p:Person) WHERE p.email = 'ada@example.test' RETURN p.id"
        ).rows == ((1,),)
        assert reopened.verify("all").findings == ()


def test_textual_ddl_and_maintenance_delegate_share_the_public_contract(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "database", page_size=512) as database:
        _seed_people(database)
        with database.begin("write") as schema:
            result = schema.execute(
                "CREATE INDEX by_score FOR (p:Person) ON (p.score) "
                "OPTIONS bucket_count = 32"
            )

        assert result.statistics["indexes_created"] == 1
        assert database.indexes.index("by_score").bucket_count == 32
        delegated = database.maintenance.create_index(
            "by_email", "Person", ["email"], bucket_count=8
        )
        assert delegated.name == "by_email"
        assert delegated.bucket_count == 8


def test_custom_seek_preserves_query_numeric_and_null_equality_semantics(
    tmp_path: Path,
) -> None:
    with connect(tmp_path / "database", page_size=512) as database:
        _seed_people(database)
        database.create_index("by_score", "Person", ("score",))

        plan = database.explain(
            "MATCH (p:Person) WHERE p.score = $score RETURN p.id"
        )
        assert any(isinstance(node, IndexSeek) for node in plan_nodes(plan))
        assert database.execute(
            "MATCH (p:Person) WHERE p.score = $score RETURN p.id",
            {"score": 7.0},
        ).rows == ((1,),)
        assert database.execute(
            "MATCH (p:Person) WHERE p.score = $score RETURN p.id",
            {"score": None},
        ).rows == ()


def test_refused_custom_creation_never_changes_durable_authority(
    tmp_path: Path,
) -> None:
    root = tmp_path / "database"
    with connect(root, page_size=512) as database:
        _seed_people(database)
        before = (
            database.wal.last_lsn,
            database._catalog.catalog.serialize(),
            tuple((item.name, item.size_bytes) for item in database.storage.files),
        )

        with pytest.raises(GrafxIndexError):
            database.create_index(
                "invalid",
                "Person",
                ("email",),
                bucket_count=8,
                expected_cardinality=1_000,
            )

        assert (
            database.wal.last_lsn,
            database._catalog.catalog.serialize(),
            tuple((item.name, item.size_bytes) for item in database.storage.files),
        ) == before
        assert all(index.name != "invalid" for index in database.indexes.registered)
        database.checkpoint()

    with connect(root, page_size=512, read_only=True) as reader:
        with pytest.raises(GrafxUnsupportedOperation):
            reader.create_index("by_email", "Person", ("email",))


@pytest.mark.parametrize(
    "unordered",
    [
        {"email", "score"},
        {"email": 1, "score": 2},
        (column for column in ("email", "score")),
    ],
)
def test_python_door_refuses_unordered_or_one_shot_columns_before_begin(
    tmp_path: Path,
    unordered: object,
) -> None:
    with connect(tmp_path / "database", page_size=512) as database:
        _seed_people(database)
        next_txn_id = database._transactions._next_txn_id

        with pytest.raises(GrafxConfigurationError) as refused:
            database.create_index("ambiguous", "Person", unordered)  # type: ignore[arg-type]

        assert refused.value.details["field"] == "columns"
        assert database._transactions._next_txn_id == next_txn_id
