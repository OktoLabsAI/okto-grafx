"""Differential contract: the planned row decoders against the canonical per-row oracle.

``_decode_tuple`` is the oracle. Every public or internal row decoder must return the same
values (bit for bit, NaN payloads included) and, for a refused payload, the same exception type,
message and details, on valid rows and on hostile ones: flipped, truncated, extended and
retagged bytes, bad UTF-8, wrong string lengths, nulls in non-nullable columns, non-finite
doubles. The same corpus drives every decoding form: full, projected (positions preserved),
landing (vectors validated, not built) and the relationship endpoint projection.
"""

from __future__ import annotations

import random
import struct
from collections.abc import Callable, Iterable

import pytest

from okto_grafx.domain.model import schema as schema_module
from okto_grafx.domain.model.schema import (
    ColumnDef,
    TableDef,
    _decode_tuple,
    _decode_tuple_fast,
    _decode_tuple_projection,
    decode_relationship_endpoints,
    decode_tuple,
    decode_tuple_landing,
    decode_tuples,
    encode_tuple,
)
from okto_grafx.domain.model.value import (
    Timestamp,
    Uuid,
    ValueType,
    VectorValue,
    encode_value,
)

SEED = 0x6A9F_D0DE
_STRINGS = (
    "",
    "a",
    "node-17",
    "área-\U0001f642",
    "x" * 300,
    "ünï" * 40,
    "line one\nline two",
)
_TAGS = tuple(sorted({int(kind) for kind in ValueType} | {0, 1, 2, 3, 99, 200, 255}))


# --------------------------------------------------------------------------------- fixtures --


def _node(table_id: int, name: str, columns: tuple[ColumnDef, ...]) -> TableDef:
    return TableDef(
        table_id=table_id, name=name, kind="node", columns=columns, primary_key=columns[0].name
    )


def _tables() -> dict[str, TableDef]:
    """Tables shaped like the graph workloads: fixed runs, strings, nullables, vectors, maps."""
    wide = _node(
        1,
        "Wide",
        (
            ColumnDef("id", ValueType.STRING, nullable=False),
            ColumnDef("label", ValueType.STRING, nullable=False),
            ColumnDef("name", ValueType.STRING),
            ColumnDef("body", ValueType.STRING),
            ColumnDef("weight", ValueType.DOUBLE, nullable=False),
            ColumnDef("count", ValueType.INT64, nullable=False),
            ColumnDef("flag", ValueType.BOOL, nullable=False),
            ColumnDef("extra", ValueType.INT64),
            ColumnDef("when", ValueType.TIMESTAMP),
            ColumnDef("embedding", ValueType.VECTOR_F32, vector_space="emb"),
            ColumnDef("props", ValueType.MAP),
        ),
    )
    fixed = _node(
        2,
        "Fixed",
        (
            ColumnDef("a", ValueType.INT64, nullable=False),
            ColumnDef("b", ValueType.DOUBLE, nullable=False),
            ColumnDef("c", ValueType.BOOL, nullable=False),
            ColumnDef("d", ValueType.INT64, nullable=False),
            ColumnDef("e", ValueType.INT64, nullable=False),
            ColumnDef("f", ValueType.DOUBLE, nullable=False),
        ),
    )
    nullable_fixed = _node(
        3,
        "NullableFixed",
        (
            ColumnDef("a", ValueType.INT64),
            ColumnDef("b", ValueType.DOUBLE),
            ColumnDef("c", ValueType.BOOL),
            ColumnDef("d", ValueType.INT64, nullable=False),
            ColumnDef("e", ValueType.DOUBLE),
        ),
    )
    strings = _node(
        4,
        "Strings",
        (
            ColumnDef("a", ValueType.STRING, nullable=False),
            ColumnDef("b", ValueType.STRING),
            ColumnDef("c", ValueType.STRING, nullable=False),
        ),
    )
    compound = _node(
        5,
        "Compound",
        (
            ColumnDef("id", ValueType.STRING, nullable=False),
            ColumnDef("raw", ValueType.BYTES),
            ColumnDef("items", ValueType.LIST),
            ColumnDef("uuid", ValueType.UUID),
            ColumnDef("n", ValueType.INT64, nullable=False),
            ColumnDef("mapping", ValueType.MAP),
        ),
    )
    rel = TableDef(
        table_id=6,
        name="Edge",
        kind="rel",
        columns=(
            ColumnDef("note", ValueType.STRING, nullable=False),
            ColumnDef("score", ValueType.DOUBLE, nullable=False),
            ColumnDef("seen", ValueType.INT64),
        ),
        from_table="Wide",
        to_table="Wide",
    )
    any_column = _node(
        7,
        "Open",
        (
            ColumnDef("id", ValueType.STRING, nullable=False),
            ColumnDef("free", schema_module.SchemaType.ANY),
            ColumnDef("n", ValueType.INT64, nullable=False),
        ),
    )
    return {
        table.name: table for table in (wide, fixed, nullable_fixed, strings, compound, rel, any_column)
    }


def _value(column: ColumnDef, rnd: random.Random) -> object:
    if column.nullable and rnd.random() < 0.25:
        return None
    kind = column.type
    if kind is ValueType.INT64:
        return rnd.choice((0, 1, -1, 2**63 - 1, -(2**63), rnd.randrange(-(10**9), 10**9)))
    if kind is ValueType.DOUBLE:
        return rnd.choice((0.0, -0.0, 1.5, 1e300, rnd.uniform(-1e6, 1e6)))
    if kind is ValueType.BOOL:
        return rnd.random() < 0.5
    if kind is ValueType.STRING:
        return rnd.choice(_STRINGS)
    if kind is ValueType.BYTES:
        return rnd.randbytes(rnd.randrange(0, 40))
    if kind is ValueType.TIMESTAMP:
        return Timestamp(rnd.randrange(0, 2**40))
    if kind is ValueType.UUID:
        return Uuid(rnd.randbytes(16))
    if kind is ValueType.LIST:
        return ("n", rnd.randrange(100), None, (1, 2.5, "z"))
    if kind is ValueType.MAP:
        return {"k": rnd.randrange(9), "s": rnd.choice(_STRINGS), 7: (1, 2)}
    if kind is ValueType.VECTOR_F32:
        return VectorValue(tuple(rnd.uniform(-1, 1) for _ in range(6)), 1)
    raise AssertionError(kind)


def _rows(table: TableDef, rnd: random.Random, count: int) -> list[tuple[object, ...]]:
    rows = []
    for _ in range(count):
        row = []
        for position, column in enumerate(table.columns):
            if table.kind == "rel" and position < 2:
                row.append(rnd.randrange(1, 10**6))
            elif column.type is schema_module.SchemaType.ANY:
                row.append(rnd.choice((None, 3, "free", (1, 2), {"x": 1})))
            else:
                row.append(_value(column, rnd))
        rows.append(tuple(row))
    return rows


# ------------------------------------------------------------------------------- comparison --


def _canon(value: object) -> object:
    """Bit-exact, order-preserving fingerprint of a decoded value (NaN safe)."""
    if isinstance(value, float):
        return ("float", struct.pack("<d", value))
    if isinstance(value, (tuple, list)):
        return (type(value).__name__, tuple(_canon(item) for item in value))
    if isinstance(value, dict):
        return ("dict", tuple((_canon(k), _canon(v)) for k, v in value.items()))
    if schema_module._is_unmaterialized_column(value):
        return ("unmaterialized",)
    if isinstance(value, VectorValue):
        width = "<f" if value.dtype == "float32" else "<d"
        return ("vector", tuple(struct.pack(width, c) for c in value.values), value.space_ref, value.dtype)
    return (type(value).__name__, repr(value))


def _outcome(call: Callable[[], object]) -> tuple[str, object]:
    try:
        return "value", _canon(call())
    except Exception as failure:  # noqa: BLE001 - the oracle defines every refusal
        return "error", (
            type(failure),
            str(failure),
            _canon(dict(getattr(failure, "details", {}) or {})),
        )


def _payload_segments(table: TableDef, row: tuple[object, ...]) -> list[bytes] | None:
    """Per-column encodings when the table encodes column by column (no DECIMAL/typed column)."""
    if any(column.stored_type is not None or column.type is ValueType.DECIMAL for column in table.columns):
        return None
    segments = [encode_value(value) for value in row]  # type: ignore[arg-type]
    return segments if b"".join(segments) == encode_tuple(table, row) else None  # type: ignore[arg-type]


def _hostile(table: TableDef, row: tuple[object, ...], rnd: random.Random) -> Iterable[bytes]:
    payload = encode_tuple(table, row)  # type: ignore[arg-type]
    segments = _payload_segments(table, row)
    for _ in range(40):  # flipped bytes
        changed = bytearray(payload)
        changed[rnd.randrange(len(changed))] ^= rnd.randrange(1, 256)
        yield bytes(changed)
    for _ in range(25):  # truncated
        yield payload[: rnd.randrange(len(payload) + 1)]
    for _ in range(15):  # extended
        yield payload + rnd.randbytes(rnd.randrange(1, 9))
    for _ in range(10):  # noise
        yield rnd.randbytes(rnd.randrange(0, len(payload) + 1))
    yield b""
    if segments is None:
        return
    starts, at = [], 0
    for segment in segments:
        starts.append(at)
        at += len(segment)
    for position, start in enumerate(starts):
        for tag in rnd.sample(_TAGS, 6):  # retag one column (incl. NULL in non-nullable ones)
            changed = bytearray(payload)
            changed[start] = tag
            yield bytes(changed)
        changed = bytearray(payload)
        changed[start] = int(ValueType.NULL)
        yield bytes(changed)
        segment = segments[position]
        if len(segment) > 1:  # mutate this column's body, or cut inside it
            changed = bytearray(payload)
            changed[start + rnd.randrange(1, len(segment))] ^= rnd.randrange(1, 256)
            yield bytes(changed)
            yield payload[: start + rnd.randrange(1, len(segment))]
        column = table.columns[position]
        if column.type is ValueType.STRING and row[position] is not None:
            for bad in (b"\xff\xfe", b"\xc0\x80", b"\xed\xa0\x80", b"\xe2\x82"):  # invalid UTF-8
                body = bad.ljust(max(len(row[position].encode("utf-8")), len(bad)), b"a")  # type: ignore[union-attr]
                rebuilt = bytes((int(ValueType.STRING),)) + struct.pack("<I", len(body)) + body
                yield payload[:start] + rebuilt + payload[start + len(segment):]
            for length in (0, 1, 2**31, 2**32 - 1, len(segment)):  # wrong lengths
                changed = bytearray(payload)
                changed[start + 1 : start + 5] = struct.pack("<I", length)
                yield bytes(changed)
        if column.type is ValueType.DOUBLE and row[position] is not None:
            for special in (float("nan"), float("inf"), float("-inf")):  # non-finite doubles
                changed = bytearray(payload)
                changed[start + 1 : start + 9] = struct.pack("<d", special)
                yield bytes(changed)
        if column.type is ValueType.BOOL and row[position] is not None:
            for raw in (2, 255):  # BOOL body above 1
                changed = bytearray(payload)
                changed[start + 1] = raw
                yield bytes(changed)


def _position_sets(table: TableDef, rnd: random.Random) -> list[frozenset[int]]:
    count = len(table.columns)
    return [
        frozenset(),
        frozenset(range(count)),
        frozenset((0,)),
        frozenset((count - 1,)),
        frozenset(rnd.sample(range(count), max(1, count // 2))),
    ]


# -------------------------------------------------------------------- the decoding forms --


def _forms(table: TableDef, rnd: random.Random):
    """(name, oracle(buf), candidate(buf)) for every decoding form over this table."""
    yield (
        "full",
        lambda buf: _decode_tuple(table, buf, materialized_positions=None),
        lambda buf: decode_tuple(table, buf),
    )
    yield (
        "landing",
        lambda buf: _decode_tuple(table, buf, materialized_positions=None, materialize_vectors=False),
        lambda buf: decode_tuple_landing(table, buf),
    )
    for materialize_vectors in (True, False):
        for preserve in (False, True):
            for positions in (None, *_position_sets(table, rnd)):
                if positions is None and preserve:
                    continue
                yield (
                    "universal[mp=%s,vec=%s,keep=%s]" % (
                        None if positions is None else sorted(positions), materialize_vectors, preserve),
                    lambda buf, p=positions, v=materialize_vectors, k=preserve: _decode_tuple(
                        table, buf, materialized_positions=p, materialize_vectors=v, preserve_positions=k
                    ),
                    lambda buf, p=positions, v=materialize_vectors, k=preserve: _decode_tuple_fast(
                        table, buf, materialized_positions=p, materialize_vectors=v, preserve_positions=k
                    ),
                )
    for positions in _position_sets(table, rnd):
        yield (
            "projection%s" % sorted(positions),
            lambda buf, p=positions: _decode_tuple(
                table, buf, materialized_positions=p, preserve_positions=True
            ),
            lambda buf, p=positions: _decode_tuple_projection(table, buf, p),
        )
    if table.kind == "rel":
        yield (
            "endpoints",
            lambda buf: _decode_tuple(
                table, buf, materialized_positions=frozenset((0, 1))
            ),
            lambda buf: decode_relationship_endpoints(table, buf),
        )


# ----------------------------------------------------------------------------------- tests --


@pytest.mark.parametrize("name", sorted(_tables()))
def test_valid_rows_decode_identically_in_every_form(name: str) -> None:
    table = _tables()[name]
    rnd = random.Random(SEED + table.table_id)
    cases = 0
    for row in _rows(table, rnd, 250):
        payload = encode_tuple(table, row)  # type: ignore[arg-type]
        for form, oracle, candidate in _forms(table, rnd):
            want = _outcome(lambda: oracle(payload))
            got = _outcome(lambda: candidate(payload))
            assert got == want, (name, form, row)
            cases += 1
    assert cases >= 250


@pytest.mark.parametrize("name", sorted(_tables()))
def test_hostile_payloads_are_refused_exactly_as_the_oracle_refuses_them(name: str) -> None:
    table = _tables()[name]
    rnd = random.Random(SEED ^ table.table_id)
    cases = refusals = 0
    for row in _rows(table, rnd, 40):
        hostile = list(_hostile(table, row, rnd))
        for form, oracle, candidate in _forms(table, rnd):
            for buf in hostile:
                want = _outcome(lambda: oracle(buf))
                got = _outcome(lambda: candidate(buf))
                assert got == want, (name, form, buf)
                cases += 1
                refusals += want[0] == "error"
    assert cases >= 2_000
    assert refusals >= 500, "the corpus must exercise the refusal paths, not only valid rows"


@pytest.mark.parametrize("name", sorted(_tables()))
def test_the_planned_path_really_accepts_valid_rows(name: str) -> None:
    """The differential tests would pass vacuously if every row fell back to the oracle."""
    table = _tables()[name]
    plan = table._fast_decode_plan
    if name == "Open":  # an ANY column: the oracle decodes every row of this table
        assert plan is None
        return
    assert plan is not None
    rnd = random.Random(SEED + 99)
    column_count = len(table.columns)
    for row in _rows(table, rnd, 100):
        payload = encode_tuple(table, row)  # type: ignore[arg-type]
        for mat in (None, tuple(position % 2 == 0 for position in range(column_count))):
            for preserve in (False, True):
                if mat is None and preserve:
                    continue
                for vectors in (True, False):
                    assert (
                        schema_module._fast_row(plan, payload, mat, column_count, vectors, preserve)
                        is not None
                    ), (name, row, mat, preserve, vectors)


def _stream(call: Callable[[], Iterable[object]]) -> tuple[list[object], tuple[str, object] | None]:
    """Everything a stream yields before it stops, and the refusal that stopped it (if any)."""
    seen: list[object] = []
    try:
        for item in call():
            seen.append(_canon(item))
    except Exception as failure:  # noqa: BLE001
        return seen, ("error", (type(failure), str(failure), _canon(dict(getattr(failure, "details", {}) or {}))))
    return seen, None


@pytest.mark.parametrize("name", sorted(_tables()))
def test_the_batch_decoder_streams_rows_and_the_first_refusal_like_row_by_row_decoding(name: str) -> None:
    table = _tables()[name]
    rnd = random.Random(SEED + 7 * table.table_id)
    column_count = len(table.columns)
    forms = [(None, True, False), (None, False, False)]
    forms += [(positions, True, True) for positions in _position_sets(table, rnd)]
    rows = _rows(table, rnd, 12)
    for round_number in range(60):
        batch: list[bytes] = []
        for row in rnd.sample(rows, 4):
            batch.append(encode_tuple(table, row))  # type: ignore[arg-type]
            if rnd.random() < 0.4:
                batch.extend(rnd.sample(list(_hostile(table, row, rnd)), 1))
        for positions, vectors, preserve in forms:
            want = _stream(
                lambda: (
                    _decode_tuple(
                        table, buf, materialized_positions=positions,
                        materialize_vectors=vectors, preserve_positions=preserve,
                    )
                    for buf in batch
                )
            )
            got = _stream(
                lambda: decode_tuples(
                    table, iter(batch), materialized_positions=positions,
                    materialize_vectors=vectors, preserve_positions=preserve,
                )
            )
            assert got == want, (name, round_number, positions, vectors, preserve)
    assert column_count > 0


def test_the_batch_decoder_reads_one_payload_per_row_and_never_ahead() -> None:
    table = _tables()["Fixed"]
    rnd = random.Random(SEED)
    payloads = [encode_tuple(table, row) for row in _rows(table, rnd, 5)]  # type: ignore[arg-type]
    pulled: list[int] = []

    def feed() -> Iterable[bytes]:
        for index, payload in enumerate(payloads):
            pulled.append(index)
            yield payload

    stream = decode_tuples(table, feed())
    assert pulled == []
    next(stream)
    assert pulled == [0]
    next(stream)
    assert pulled == [0, 1]


def test_vector_components_decode_bit_for_bit_including_nan_payloads_and_negative_zero() -> None:
    """The decoder does not re-judge components (SPEC-VEC BR-5): every bit pattern survives."""
    table = _tables()["Wide"]
    rnd = random.Random(SEED + 31)
    patterns = (
        0x7FC00000, 0x7FC00001, 0xFFC12345, 0x7F800001, 0x7F800000, 0xFF800000,  # NaNs and infinities
        0x80000000, 0x00000000, 0x00000001, 0x007FFFFF, 0x3F800000, 0xBF800000,  # -0.0, 0.0, subnormals, +-1
    )
    checked = 0
    for row in _rows(table, rnd, 40):
        if row[-2] is None:  # the embedding column is the second to last
            continue
        segments = _payload_segments(table, row)
        assert segments is not None
        start = sum(len(segment) for segment in segments[:-2])
        body = start + 1 + 8  # tag, dimension, space reference
        dimension = len(row[-2].values)  # type: ignore[union-attr]
        payload = bytearray(encode_tuple(table, row))  # type: ignore[arg-type]
        for component in range(dimension):
            payload[body + 4 * component : body + 4 * component + 4] = struct.pack("<I", rnd.choice(patterns))
        for form, oracle, candidate in _forms(table, rnd):
            want = _outcome(lambda: oracle(bytes(payload)))
            got = _outcome(lambda: candidate(bytes(payload)))
            assert got == want, form
            checked += 1
        decoded = decode_tuple(table, bytes(payload))
        reference = struct.unpack_from("<%df" % dimension, bytes(payload), body)  # the C reference
        assert [struct.pack("<d", c) for c in decoded[-2].values] == [  # type: ignore[union-attr]
            struct.pack("<d", c) for c in reference
        ]
    assert checked > 200
