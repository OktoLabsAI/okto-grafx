"""The in-memory catalog: what it accepts, what it refuses, and what it serialises to.

The rule the tests are built around is SPEC-VEC TR-2: the identity of an embedding space is
immutable after creation, and the only sanctioned change is from active to retired. Every other
way of altering an installed space has to be refused, or a stored vector could end up pointing
at a space whose dimension no longer matches it.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import (
    GrafxConfigurationError,
    GrafxCorruptionDetected,
    GrafxSchemaVersionMismatch,
    GrafxSpaceRetired,
)
from okto_grafx.domain.model.catalog import CATALOG_FORMAT_VERSION, CATALOG_MAGIC, Catalog
from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.domain.ports.vectormath import DistanceMetric


def space(name: str = "minilm", space_id: int = 1, **overrides: object) -> EmbeddingSpaceDef:
    """Return an active embedding space with the fields these tests reuse."""
    fields: dict[str, object] = {
        "space_id": space_id,
        "name": name,
        "dimension": 384,
        "metric": DistanceMetric.COSINE,
        "normalized": True,
        "storage_dtype": "float32",
        "created_at_wall": 1_700_000_000.5,
    }
    fields.update(overrides)
    return EmbeddingSpaceDef(**fields)  # type: ignore[arg-type]


def table(name: str = "Person", table_id: int = 1, **overrides: object) -> TableDef:
    """Return a node table with the fields these tests reuse."""
    fields: dict[str, object] = {
        "table_id": table_id,
        "name": name,
        "kind": "node",
        "columns": (
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="name", type=ValueType.STRING),
        ),
        "primary_key": "id",
    }
    fields.update(overrides)
    return TableDef(**fields)  # type: ignore[arg-type]


def test_an_empty_catalog_starts_its_identifiers_at_one() -> None:
    catalog = Catalog()
    assert catalog.is_empty()
    assert catalog.tables() == ()
    assert catalog.spaces() == ()
    assert catalog.next_table_id() == 1
    assert catalog.next_space_id() == 1


def test_adding_a_table_makes_it_findable_by_name_and_by_id() -> None:
    catalog = Catalog()
    added = catalog.add_table(table())
    assert catalog.table("Person") is added
    assert catalog.table_by_id(1) is added
    assert catalog.has_table("Person")
    assert not catalog.has_table("Missing")
    assert catalog.tables() == (added,)
    assert catalog.next_table_id() == 2
    assert not catalog.is_empty()


def test_a_missing_table_is_a_typed_refusal() -> None:
    catalog = Catalog()
    with pytest.raises(GrafxConfigurationError):
        catalog.table("Missing")
    with pytest.raises(GrafxCorruptionDetected):
        catalog.table_by_id(99)
    with pytest.raises(GrafxConfigurationError):
        catalog.space("Missing")
    with pytest.raises(GrafxCorruptionDetected):
        catalog.space_by_id(99)


def test_a_duplicate_table_name_or_id_is_refused() -> None:
    catalog = Catalog()
    catalog.add_table(table())
    with pytest.raises(GrafxConfigurationError):
        catalog.add_table(table(table_id=2))
    with pytest.raises(GrafxConfigurationError):
        catalog.add_table(table(name="Other"))
    assert len(catalog.tables()) == 1


def test_a_catalog_holds_only_schema_values() -> None:
    catalog = Catalog()
    with pytest.raises(GrafxConfigurationError):
        catalog.add_table("Person")  # type: ignore[arg-type]
    with pytest.raises(GrafxConfigurationError):
        catalog.add_space("minilm")  # type: ignore[arg-type]


def test_the_next_identifier_follows_the_highest_in_use() -> None:
    catalog = Catalog()
    catalog.add_table(table(table_id=17))
    catalog.add_space(space(space_id=9))
    assert catalog.next_table_id() == 18
    assert catalog.next_space_id() == 10


# --- embedding spaces -------------------------------------------------------------------------


def test_a_space_is_born_active_and_can_be_retired_once() -> None:
    catalog = Catalog()
    catalog.add_space(space())
    assert catalog.space("minilm").is_active
    retired = catalog.retire_space("minilm")
    assert not retired.is_active
    assert catalog.space("minilm").state == "retired"
    with pytest.raises(GrafxSpaceRetired):
        catalog.retire_space("minilm")


def test_retiring_a_space_that_does_not_exist_is_refused() -> None:
    with pytest.raises(GrafxConfigurationError):
        Catalog().retire_space("missing")


def test_a_space_may_not_be_created_already_retired() -> None:
    catalog = Catalog()
    with pytest.raises(GrafxConfigurationError) as raised:
        catalog.add_space(space(state="retired"))
    assert raised.value.details["field"] == "state"


def test_a_space_name_and_id_are_unique_and_immutable() -> None:
    catalog = Catalog()
    catalog.add_space(space())
    with pytest.raises(GrafxConfigurationError):
        catalog.add_space(space(space_id=2))
    with pytest.raises(GrafxConfigurationError):
        catalog.add_space(space(name="other"))
    # Re-adding an identical definition is still a duplicate, not an idempotent update.
    with pytest.raises(GrafxConfigurationError):
        catalog.add_space(space())
    # And a redefinition with a different dimension cannot get in through any door.
    with pytest.raises(GrafxConfigurationError):
        catalog.add_space(space(dimension=768))
    assert catalog.space("minilm").dimension == 384


def test_a_vector_column_must_point_at_a_space_that_exists() -> None:
    catalog = Catalog()
    vector_table = table(
        name="Chunk",
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="embedding", type=ValueType.VECTOR_F32, vector_space="minilm"),
        ),
    )
    with pytest.raises(GrafxConfigurationError) as raised:
        catalog.add_table(vector_table)
    assert raised.value.details["field"] == "vector_space"
    catalog.add_space(space())
    assert catalog.add_table(vector_table) is vector_table


def test_a_vector_column_may_not_point_at_a_retired_space() -> None:
    catalog = Catalog()
    catalog.add_space(space())
    catalog.retire_space("minilm")
    with pytest.raises(GrafxSpaceRetired):
        catalog.add_table(
            table(
                name="Chunk",
                columns=(
                    ColumnDef(name="embedding", type=ValueType.VECTOR_F32, vector_space="minilm"),
                ),
                primary_key=None,
            )
        )


def test_a_vector_column_must_match_the_precision_of_its_space() -> None:
    catalog = Catalog()
    catalog.add_space(space(storage_dtype="float64"))
    with pytest.raises(GrafxConfigurationError) as raised:
        catalog.add_table(
            table(
                name="Chunk",
                columns=(
                    ColumnDef(name="embedding", type=ValueType.VECTOR_F32, vector_space="minilm"),
                ),
                primary_key=None,
            )
        )
    assert raised.value.details["field"] == "storage_dtype"


# --- serialisation ---------------------------------------------------------------------------


def populated() -> Catalog:
    """Return a catalog with two spaces, one of them retired, and three tables."""
    catalog = Catalog()
    catalog.add_space(space())
    catalog.add_space(space(name="e5", space_id=2, dimension=1024, storage_dtype="float64"))
    catalog.retire_space("e5")
    catalog.add_table(table())
    catalog.add_table(
        table(
            name="Chunk",
            table_id=2,
            columns=(
                ColumnDef(name="id", type=ValueType.INT64, nullable=False),
                ColumnDef(name="layer", type=ValueType.STRING),
                ColumnDef(name="embedding", type=ValueType.VECTOR_F32, vector_space="minilm"),
            ),
        )
    )
    catalog.add_table(
        TableDef(
            table_id=3,
            name="BelongsTo",
            kind="rel",
            columns=(ColumnDef(name="weight", type=ValueType.DOUBLE),),
            from_table="Chunk",
            to_table="Person",
            schema_version=4,
        )
    )
    return catalog


def test_a_catalog_round_trips_through_its_bytes() -> None:
    catalog = populated()
    raw = catalog.serialize()
    restored = Catalog.deserialize(raw)
    assert restored == catalog
    assert restored.tables() == catalog.tables()
    assert restored.spaces() == catalog.spaces()
    assert restored.next_table_id() == catalog.next_table_id()
    assert restored.next_space_id() == catalog.next_space_id()


def test_an_empty_catalog_round_trips() -> None:
    assert Catalog.deserialize(Catalog().serialize()) == Catalog()


def test_serialisation_is_stable_for_the_same_catalog() -> None:
    assert populated().serialize() == populated().serialize()


def test_the_serialised_form_opens_with_its_magic_and_version() -> None:
    raw = populated().serialize()
    assert raw.startswith(CATALOG_MAGIC)
    assert int.from_bytes(raw[8:10], "little") == CATALOG_FORMAT_VERSION


def test_a_retired_space_stays_retired_across_a_round_trip() -> None:
    restored = Catalog.deserialize(populated().serialize())
    assert not restored.space("e5").is_active
    assert restored.space("minilm").is_active
    with pytest.raises(GrafxSpaceRetired):
        restored.retire_space("e5")


def test_every_field_of_a_space_survives_the_round_trip() -> None:
    restored = Catalog.deserialize(populated().serialize())
    original = populated().space("e5")
    assert restored.space("e5") == original
    assert restored.space("e5").created_at_wall == original.created_at_wall
    assert restored.space("e5").metric is DistanceMetric.COSINE
    assert restored.space("e5").storage_dtype == "float64"


def test_every_field_of_a_table_survives_the_round_trip() -> None:
    restored = Catalog.deserialize(populated().serialize())
    relationship = restored.table("BelongsTo")
    assert relationship.kind == "rel"
    assert relationship.from_table == "Chunk"
    assert relationship.to_table == "Person"
    assert relationship.schema_version == 4
    chunk = restored.table("Chunk")
    assert chunk.column("embedding").vector_space == "minilm"
    assert chunk.column("id").nullable is False


def test_a_flipped_byte_fails_the_catalog_checksum() -> None:
    raw = bytearray(populated().serialize())
    raw[30] ^= 0x01
    with pytest.raises(GrafxCorruptionDetected) as raised:
        Catalog.deserialize(bytes(raw))
    assert raised.value.details["field"] in {"checksum", "magic", "text"}


def test_a_truncated_catalog_is_refused() -> None:
    raw = populated().serialize()
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(raw[:20])
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(b"")


def test_foreign_bytes_are_refused() -> None:
    raw = bytearray(populated().serialize())
    raw[0:8] = b"NOTACTLG"
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(bytes(raw))


def test_a_catalog_from_a_future_format_is_refused() -> None:
    raw = bytearray(populated().serialize())
    raw[8:10] = (99).to_bytes(2, "little")
    with pytest.raises(GrafxSchemaVersionMismatch):
        Catalog.deserialize(bytes(raw))


def test_a_catalog_with_extra_trailing_bytes_is_refused() -> None:
    raw = populated().serialize()
    with pytest.raises(GrafxCorruptionDetected):
        Catalog.deserialize(raw + b"\x00" * 4)


def test_the_repr_says_how_much_it_holds() -> None:
    assert repr(populated()) == "Catalog(tables=3, spaces=2)"


def test_a_catalog_is_never_equal_to_something_else() -> None:
    assert Catalog() != "catalog"
    assert Catalog() == Catalog()
