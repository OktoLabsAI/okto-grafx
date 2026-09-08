"""Differential contract for the closed projected-row decoder (KGRUN-1/NATVER-1)."""

from __future__ import annotations

import random

from okto_grafx.domain.model.schema import (
    ColumnDef,
    TableDef,
    _decode_tuple_projection,
    _is_unmaterialized_column,
    decode_tuple,
    encode_tuple,
)
from okto_grafx.domain.model.value import Timestamp, Uuid, ValueType, VectorValue


def _table_and_row() -> tuple[TableDef, tuple[object, ...]]:
    table = TableDef(
        table_id=91,
        name="Projected",
        kind="node",
        columns=(
            ColumnDef("id", ValueType.STRING, nullable=False),
            ColumnDef("flag", ValueType.BOOL),
            ColumnDef("number", ValueType.INT64),
            ColumnDef("score", ValueType.DOUBLE),
            ColumnDef("text", ValueType.STRING),
            ColumnDef("raw", ValueType.BYTES),
            ColumnDef("items", ValueType.LIST),
            ColumnDef("mapping", ValueType.MAP),
            ColumnDef("when", ValueType.TIMESTAMP),
            ColumnDef("uuid", ValueType.UUID),
            ColumnDef("embedding", ValueType.VECTOR_F32, vector_space="emb"),
        ),
        primary_key="id",
    )
    row = (
        "node-1",
        True,
        17,
        0.25,
        "área-\U0001f642",
        b"payload\x00tail",
        ("nested", 9, None, {"inside": False}),
        {"source": "pulse", 3: (1, 2)},
        Timestamp(1_777_777_777),
        Uuid(bytes(range(16))),
        VectorValue((0.5, -1.0, 2.0, 0.25), 1),
    )
    return table, row


def _outcome(operation) -> tuple[str, object]:
    try:
        return "value", operation()
    except Exception as failure:  # noqa: BLE001 - the canonical decoder is the oracle
        details = getattr(failure, "details", {})
        return "error", (type(failure), details.get("field"), details.get("offset"))


def _hostile_corpus(payload: bytes) -> list[bytes]:
    randomizer = random.Random(0xDEC0DE)
    cases: list[bytes] = []
    for _ in range(5_000):
        changed = bytearray(payload)
        at = randomizer.randrange(len(changed))
        changed[at] ^= randomizer.randrange(1, 256)
        cases.append(bytes(changed))
    for _ in range(2_000):
        cases.append(payload[: randomizer.randrange(len(payload) + 1)])
    for _ in range(1_000):
        cases.append(payload + randomizer.randbytes(randomizer.randrange(1, 9)))
    for _ in range(2_000):
        size = randomizer.randrange(len(payload) + 1)
        cases.append(randomizer.randbytes(size))
    return cases


def test_projected_decode_preserves_positions_without_publishing_omitted_values() -> None:
    table, row = _table_and_row()
    payload = encode_tuple(table, row)  # type: ignore[arg-type]
    positions = frozenset((0, 2, 5, 10))

    projected = _decode_tuple_projection(table, payload, positions)

    assert len(projected) == len(row)
    for position, expected in enumerate(row):
        if position in positions:
            assert projected[position] == expected
        else:
            assert _is_unmaterialized_column(projected[position])


def test_projected_decode_matches_the_canonical_oracle_on_10k_hostile_payloads() -> None:
    table, row = _table_and_row()
    payload = encode_tuple(table, row)  # type: ignore[arg-type]
    positions = frozenset((0, 2, 5, 10))

    for hostile in _hostile_corpus(payload):
        canonical = _outcome(lambda: decode_tuple(table, hostile))
        projected = _outcome(
            lambda: _decode_tuple_projection(table, hostile, positions)
        )
        assert projected[0] == canonical[0]
        if canonical[0] == "error":
            assert projected == canonical
            continue
        full = canonical[1]
        partial = projected[1]
        assert isinstance(full, tuple) and isinstance(partial, tuple)
        for position in positions:
            assert partial[position] == full[position]
        for position in set(range(len(row))) - positions:
            assert _is_unmaterialized_column(partial[position])
