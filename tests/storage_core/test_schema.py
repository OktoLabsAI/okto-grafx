"""Schema definitions and the positional tuple encoding (CONTRACT.md section 7.2).

A schema object validates itself when it is built, so the tests here are mostly about what is
refused. The reason is FR-13 of the storage side of the contract in spirit rather than in
letter: an impossible table that reaches the catalog becomes an impossible file, and by then the
error has no owner.

The tuple encoding has one rule that is worth more than the rest: it never coerces. A value of
the wrong type is a refusal with the column named, not an integer quietly turned into a double.
"""

from __future__ import annotations

import pytest

from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected
from okto_grafx.domain.model.errors import SchemaMismatchError
from okto_grafx.domain.model.schema import (
    MAX_IDENTIFIER_LENGTH,
    SPACE_STATE_ACTIVE,
    SPACE_STATE_RETIRED,
    ColumnDef,
    EmbeddingSpaceDef,
    TableDef,
    decode_tuple,
    encode_tuple,
    is_identifier,
)
from okto_grafx.domain.model.value import Timestamp, Uuid, ValueType, VectorValue
from okto_grafx.domain.ports.vectormath import DistanceMetric


def person_table(**overrides: object) -> TableDef:
    """Return a node table with the fields these tests reuse."""
    fields: dict[str, object] = {
        "table_id": 1,
        "name": "Person",
        "kind": "node",
        "columns": (
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="name", type=ValueType.STRING),
            ColumnDef(name="score", type=ValueType.DOUBLE),
        ),
        "primary_key": "id",
    }
    fields.update(overrides)
    return TableDef(**fields)  # type: ignore[arg-type]


# --- identifiers ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["a", "Person", "_private", "table_1", "A" * MAX_IDENTIFIER_LENGTH])
def test_an_accepted_identifier(name: str) -> None:
    assert is_identifier(name)


@pytest.mark.parametrize(
    "name", ["", "1table", "with space", "with-dash", "naive" + chr(233), "A" * 129, None, 7]
)
def test_a_refused_identifier(name: object) -> None:
    assert not is_identifier(name)


# --- columns -------------------------------------------------------------------------------


def test_a_column_carries_its_type_and_nullability() -> None:
    column = ColumnDef(name="name", type=ValueType.STRING, nullable=False)
    assert column.name == "name"
    assert column.type is ValueType.STRING
    assert not column.nullable
    assert not column.is_vector


def test_a_vector_column_must_name_a_space() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        ColumnDef(name="embedding", type=ValueType.VECTOR_F32)
    assert raised.value.details["field"] == "vector_space"
    column = ColumnDef(name="embedding", type=ValueType.VECTOR_F32, vector_space="minilm")
    assert column.is_vector


def test_a_column_that_is_not_a_vector_may_not_name_a_space() -> None:
    with pytest.raises(GrafxConfigurationError):
        ColumnDef(name="name", type=ValueType.STRING, vector_space="minilm")


@pytest.mark.parametrize(
    ("field", "value"),
    [("name", ""), ("name", "1bad"), ("type", "INT64"), ("nullable", "yes")],
)
def test_an_unusable_column_is_refused(field: str, value: object) -> None:
    fields: dict[str, object] = {"name": "ok", "type": ValueType.INT64, "nullable": True}
    fields[field] = value
    with pytest.raises(GrafxConfigurationError):
        ColumnDef(**fields)  # type: ignore[arg-type]


# --- tables --------------------------------------------------------------------------------


def test_a_node_table_is_accepted_and_answers_about_its_columns() -> None:
    table = person_table()
    assert table.arity == 3
    assert table.column("name").type is ValueType.STRING
    assert table.column_index("score") == 2
    with pytest.raises(GrafxConfigurationError):
        table.column("missing")
    with pytest.raises(GrafxConfigurationError):
        table.column_index("missing")


def test_a_relationship_table_needs_both_endpoints() -> None:
    columns = (ColumnDef(name="since", type=ValueType.INT64),)
    table = TableDef(
        table_id=2,
        name="Knows",
        kind="rel",
        columns=columns,
        from_table="Person",
        to_table="Person",
    )
    assert table.from_table == "Person"
    with pytest.raises(GrafxConfigurationError):
        TableDef(table_id=2, name="Knows", kind="rel", columns=columns, from_table="Person")
    with pytest.raises(GrafxConfigurationError):
        TableDef(table_id=2, name="Knows", kind="rel", columns=columns)


def test_a_node_table_may_not_declare_endpoints() -> None:
    with pytest.raises(GrafxConfigurationError):
        person_table(from_table="Person")


def test_a_relationship_table_may_not_declare_a_primary_key() -> None:
    with pytest.raises(GrafxConfigurationError):
        TableDef(
            table_id=2,
            name="Knows",
            kind="rel",
            columns=(ColumnDef(name="since", type=ValueType.INT64),),
            primary_key="since",
            from_table="Person",
            to_table="Person",
        )


def test_a_primary_key_must_name_a_column() -> None:
    with pytest.raises(GrafxConfigurationError) as raised:
        person_table(primary_key="missing")
    assert raised.value.details["field"] == "primary_key"


def test_a_duplicate_column_name_is_refused() -> None:
    with pytest.raises(GrafxConfigurationError):
        person_table(
            columns=(
                ColumnDef(name="id", type=ValueType.INT64),
                ColumnDef(name="id", type=ValueType.STRING),
            ),
            primary_key=None,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("table_id", 0),
        ("table_id", -1),
        ("table_id", 1 << 32),
        ("table_id", True),
        ("name", ""),
        ("kind", "edge"),
        ("columns", ()),
        ("columns", [ColumnDef(name="id", type=ValueType.INT64)]),
        ("schema_version", 0),
        ("schema_version", 1 << 16),
    ],
)
def test_an_unusable_table_is_refused(field: str, value: object) -> None:
    with pytest.raises(GrafxConfigurationError):
        person_table(**{field: value, "primary_key": None})


def test_a_table_column_that_is_not_a_column_def_is_refused() -> None:
    with pytest.raises(GrafxConfigurationError):
        person_table(columns=("id",), primary_key=None)


# --- embedding spaces -------------------------------------------------------------------------


def test_an_embedding_space_carries_its_identity() -> None:
    space = EmbeddingSpaceDef(
        space_id=1,
        name="minilm",
        dimension=384,
        metric=DistanceMetric.COSINE,
        normalized=True,
        created_at_wall=1_700_000_000.0,
    )
    assert space.is_active
    assert space.state == SPACE_STATE_ACTIVE
    assert space.value_type is ValueType.VECTOR_F32
    retired = space.retired()
    assert retired.state == SPACE_STATE_RETIRED
    assert not retired.is_active
    assert retired.space_id == space.space_id
    assert retired.dimension == space.dimension
    assert space.is_active, "retiring returns a new value and never mutates the old one"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("space_id", 0),
        ("space_id", -3),
        ("name", ""),
        ("dimension", 0),
        ("dimension", -1),
        ("dimension", 1 << 20),
        ("metric", "cosine"),
        ("normalized", "true"),
        ("storage_dtype", "float16"),
        ("state", "archived"),
        ("created_at_wall", "yesterday"),
    ],
)
def test_an_unusable_embedding_space_is_refused(field: str, value: object) -> None:
    fields: dict[str, object] = {
        "space_id": 1,
        "name": "minilm",
        "dimension": 8,
        "metric": DistanceMetric.COSINE,
        "normalized": False,
    }
    fields[field] = value
    with pytest.raises(GrafxConfigurationError):
        EmbeddingSpaceDef(**fields)  # type: ignore[arg-type]


def test_a_double_precision_space_produces_double_precision_vectors() -> None:
    space = EmbeddingSpaceDef(
        space_id=1,
        name="big",
        dimension=4,
        metric=DistanceMetric.DOT,
        normalized=False,
        storage_dtype="float64",
    )
    assert space.value_type is ValueType.VECTOR_F64


# --- tuples ----------------------------------------------------------------------------------


def test_a_tuple_round_trips_through_its_schema() -> None:
    table = person_table()
    values = (7, "Ada", 9.5)
    raw = encode_tuple(table, values)
    assert decode_tuple(table, raw) == values


def test_a_null_is_allowed_only_where_the_column_says_so() -> None:
    table = person_table()
    assert decode_tuple(table, encode_tuple(table, (7, None, 1.0))) == (7, None, 1.0)
    with pytest.raises(SchemaMismatchError) as raised:
        encode_tuple(table, (None, "Ada", 1.0))
    assert raised.value.details["column"] == "id"
    assert raised.value.details["position"] == 0


def test_a_tuple_of_the_wrong_arity_is_refused() -> None:
    table = person_table()
    for values in ((), (1,), (1, "Ada"), (1, "Ada", 1.0, "extra")):
        with pytest.raises(SchemaMismatchError) as raised:
            encode_tuple(table, values)
        assert raised.value.details["expected_arity"] == 3


def test_a_value_of_the_wrong_type_is_refused_and_never_coerced() -> None:
    table = person_table()
    with pytest.raises(SchemaMismatchError) as raised:
        encode_tuple(table, (7, 8, 9.5))
    assert raised.value.details["column"] == "name"
    assert raised.value.details["declared_type"] == "STRING"
    # An integer is not silently widened into the DOUBLE column either.
    with pytest.raises(SchemaMismatchError):
        encode_tuple(table, (7, "Ada", 9))
    # Nor is a boolean accepted where an integer is declared.
    with pytest.raises(SchemaMismatchError):
        encode_tuple(table, (True, "Ada", 9.5))


def test_something_that_is_not_a_sequence_of_values_is_refused() -> None:
    table = person_table()
    with pytest.raises(SchemaMismatchError):
        encode_tuple(table, "abc")  # type: ignore[arg-type]
    with pytest.raises(SchemaMismatchError):
        encode_tuple(table, 7)  # type: ignore[arg-type]


def test_a_payload_with_trailing_bytes_is_corruption() -> None:
    table = person_table()
    raw = encode_tuple(table, (7, "Ada", 9.5))
    with pytest.raises(GrafxCorruptionDetected):
        decode_tuple(table, raw + b"\x00")


def test_a_payload_whose_stored_type_left_the_schema_is_refused() -> None:
    table = person_table()
    other = person_table(
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=False),
            ColumnDef(name="name", type=ValueType.INT64),
            ColumnDef(name="score", type=ValueType.DOUBLE),
        )
    )
    raw = encode_tuple(table, (7, "Ada", 9.5))
    with pytest.raises(SchemaMismatchError):
        decode_tuple(other, raw)


def test_a_stored_null_in_a_column_that_forbids_it_is_refused() -> None:
    table = person_table()
    nullable = person_table(
        columns=(
            ColumnDef(name="id", type=ValueType.INT64, nullable=True),
            ColumnDef(name="name", type=ValueType.STRING),
            ColumnDef(name="score", type=ValueType.DOUBLE),
        )
    )
    raw = encode_tuple(nullable, (None, "Ada", 9.5))
    with pytest.raises(SchemaMismatchError):
        decode_tuple(table, raw)


def test_every_value_type_can_be_a_column() -> None:
    columns = (
        ColumnDef(name="a", type=ValueType.BOOL),
        ColumnDef(name="b", type=ValueType.INT64),
        ColumnDef(name="c", type=ValueType.DOUBLE),
        ColumnDef(name="d", type=ValueType.STRING),
        ColumnDef(name="e", type=ValueType.BYTES),
        ColumnDef(name="f", type=ValueType.LIST),
        ColumnDef(name="g", type=ValueType.MAP),
        ColumnDef(name="h", type=ValueType.TIMESTAMP),
        ColumnDef(name="i", type=ValueType.UUID),
        ColumnDef(name="j", type=ValueType.VECTOR_F32, vector_space="small"),
        ColumnDef(name="k", type=ValueType.VECTOR_F64, vector_space="big"),
    )
    table = TableDef(table_id=3, name="Everything", kind="node", columns=columns)
    values = (
        True,
        -5,
        0.25,
        "text",
        b"\x00\x01",
        (1, (2, 3)),
        {"key": "value"},
        Timestamp(123456789),
        Uuid(bytes(range(16))),
        VectorValue((0.5, 0.25), space_ref=1),
        VectorValue((0.1, 0.2), space_ref=2, dtype="float64"),
    )
    assert decode_tuple(table, encode_tuple(table, values)) == values


def test_a_vector_of_the_wrong_precision_does_not_fit_the_column() -> None:
    table = TableDef(
        table_id=4,
        name="Chunk",
        kind="node",
        columns=(ColumnDef(name="embedding", type=ValueType.VECTOR_F32, vector_space="small"),),
    )
    with pytest.raises(SchemaMismatchError):
        encode_tuple(table, (VectorValue((1.0,), space_ref=1, dtype="float64"),))
