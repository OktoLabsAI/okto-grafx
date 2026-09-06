"""Hashed IN parameter lists answer exactly what the linear walk answers, once per statement."""

from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import okto_grafx
from okto_grafx.engine import query_engine as engine_module
from okto_grafx.engine.query_engine import (
    _build_in_list_memo,
    _equal,
    _in_list_memo,
    _membership,
    _memo_membership,
)

ROWS = 300
IDS = tuple(f"t-{position:03d}" for position in range(ROWS))


def _context() -> SimpleNamespace:
    """The two statement fields the memo doors read, without a running statement."""
    return SimpleNamespace(in_list_memos={}, in_list_memo_elements=0)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    handle = okto_grafx.connect(str(tmp_path / "db"), page_size=8192)
    try:
        with handle.begin("write") as txn:
            txn.execute(
                "CREATE NODE TABLE T(id STRING, n INT64, d DOUBLE, PRIMARY KEY(id))"
            )
        with handle.begin("write") as txn:
            for position, identifier in enumerate(IDS):
                txn.execute(
                    "CREATE (:T {id: $id, n: $n, d: $d})",
                    {"id": identifier, "n": position, "d": float(position)},
                )
        yield handle
    finally:
        handle.close()


def _ids(database: object, text: str, parameters: dict[str, object]) -> list[str]:
    return sorted(row[0] for row in database.execute(text, parameters).rows)  # type: ignore[attr-defined]


# --- statement level: the memo never changes an answer -------------------------------------


@pytest.mark.parametrize(
    "ids",
    [
        [],
        ["t-000"],
        ["t-010", "t-020", "t-020", "t-010"],
        ["absent", "t-299", "t-150"],
        [f"t-{position:03d}" for position in range(0, ROWS, 7)],
    ],
)
def test_in_parameter_list_answers_like_the_walk(
    database: object, ids: list[str]
) -> None:
    expected = sorted(set(ids) & set(IDS))
    assert (
        _ids(database, "MATCH (n:T) WHERE n.id IN $ids RETURN n.id", {"ids": ids})
        == expected
    )
    assert _ids(
        database, "MATCH (n:T) WHERE NOT (n.id IN $ids) RETURN n.id", {"ids": ids}
    ) == sorted(set(IDS) - set(ids))


def test_null_in_the_list_keeps_a_miss_unknown(database: object) -> None:
    ids = ["t-001", None, "t-002"]
    assert _ids(
        database, "MATCH (n:T) WHERE n.id IN $ids RETURN n.id", {"ids": ids}
    ) == [
        "t-001",
        "t-002",
    ]
    # NOT unknown is unknown: no row survives, where the same NOT without the null keeps 298.
    assert (
        _ids(database, "MATCH (n:T) WHERE NOT (n.id IN $ids) RETURN n.id", {"ids": ids})
        == []
    )
    assert (
        len(
            _ids(
                database,
                "MATCH (n:T) WHERE NOT (n.id IN $ids) RETURN n.id",
                {"ids": ["t-001", "t-002"]},
            )
        )
        == ROWS - 2
    )


def test_numbers_keep_the_linear_walk_and_its_cross_type_equality(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds: list[object] = []
    original = engine_module._build_in_list_memo

    def counting(value: object, context: object) -> object:
        memo = original(value, context)
        builds.append(memo)
        return memo

    monkeypatch.setattr(engine_module, "_build_in_list_memo", counting)
    rows = _ids(
        database, "MATCH (n:T) WHERE n.n IN $ns RETURN n.id", {"ns": [1.0, 2, "3", 4.5]}
    )
    assert rows == ["t-001", "t-002"]
    assert builds == [None]
    # An all-integer list against a DOUBLE column: 1 = 1.0 holds only on the walk.
    rows = _ids(database, "MATCH (n:T) WHERE n.d IN $ns RETURN n.id", {"ns": [1, 2]})
    assert rows == ["t-001", "t-002"]
    assert builds == [None, None]


def test_memo_is_built_once_per_parameter_per_statement(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds: list[object] = []
    original = engine_module._build_in_list_memo

    def counting(value: object, context: object) -> object:
        memo = original(value, context)
        builds.append(memo)
        return memo

    walks: list[object] = []
    original_walk = engine_module._membership

    def counting_walk(left: object, right: object) -> object:
        walks.append(left)
        return original_walk(left, right)

    monkeypatch.setattr(engine_module, "_build_in_list_memo", counting)
    monkeypatch.setattr(engine_module, "_membership", counting_walk)
    ids = ["t-005", "t-006", None]
    text = "MATCH (n:T) WHERE n.id IN $ids OR n.id IN $ids RETURN n.id"
    assert _ids(database, text, {"ids": ids}) == ["t-005", "t-006"]
    assert len(builds) == 1 and builds[0] is not None
    assert walks == []
    assert _ids(database, text, {"ids": ids}) == ["t-005", "t-006"]
    assert len(builds) == 2


def test_ceiling_declines_once_and_the_walk_answers(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "_IN_LIST_MEMO_MAX_TOTAL_ELEMENTS", 8)
    builds: list[object] = []
    original = engine_module._build_in_list_memo

    def counting(value: object, context: object) -> object:
        memo = original(value, context)
        builds.append(memo)
        return memo

    walks: list[object] = []
    original_walk = engine_module._membership

    def counting_walk(left: object, right: object) -> object:
        walks.append(left)
        return original_walk(left, right)

    monkeypatch.setattr(engine_module, "_build_in_list_memo", counting)
    monkeypatch.setattr(engine_module, "_membership", counting_walk)
    ids = [f"t-{position:03d}" for position in range(20)]
    assert (
        _ids(database, "MATCH (n:T) WHERE n.id IN $ids RETURN n.id", {"ids": ids})
        == ids
    )
    assert builds == [None]
    assert len(walks) == ROWS


def test_two_parameters_share_one_statement_ceiling(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "_IN_LIST_MEMO_MAX_TOTAL_ELEMENTS", 12)
    builds: list[object] = []
    original = engine_module._build_in_list_memo

    def counting(value: object, context: object) -> object:
        memo = original(value, context)
        builds.append(memo)
        return memo

    monkeypatch.setattr(engine_module, "_build_in_list_memo", counting)
    first = [f"t-{position:03d}" for position in range(8)]
    second = [f"t-{position:03d}" for position in range(8, 16)]
    rows = _ids(
        database,
        "MATCH (n:T) WHERE n.id IN $first OR n.id IN $second RETURN n.id",
        {"first": first, "second": second},
    )
    assert rows == first + second
    assert [memo is not None for memo in builds] == [True, False]


def test_pulse_incident_scan_answers_through_the_memo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    handle = okto_grafx.connect(str(tmp_path / "kg"), page_size=8192)
    try:
        handle.ensure_identity_indexes()
        with handle.begin("write") as txn:
            txn.execute("CREATE NODE TABLE A(id STRING, PRIMARY KEY(id))")
            txn.execute("CREATE NODE TABLE B(id STRING, PRIMARY KEY(id))")
            txn.execute("CREATE REL TABLE R(FROM A TO B, confidence DOUBLE)")
        handle.ensure_identity_indexes()
        with handle.begin("write") as txn:
            for position in range(12):
                txn.execute("CREATE (:A {id: $id})", {"id": f"a{position}"})
                txn.execute("CREATE (:B {id: $id})", {"id": f"b{position}"})
            for position in range(12):
                txn.execute(
                    "MATCH (a:A {id: $s}), (b:B {id: $t}) "
                    "CREATE (a)-[:R {confidence: $c}]->(b)",
                    {"s": f"a{position}", "t": f"b{(position * 5) % 12}", "c": 0.5},
                )
        walks: list[object] = []
        original_walk = engine_module._membership

        def counting_walk(left: object, right: object) -> object:
            walks.append(left)
            return original_walk(left, right)

        monkeypatch.setattr(engine_module, "_membership", counting_walk)
        # 12 allocated edges against 40 endpoint keys selects the edge-first scan, whose
        # predicate is exactly the Pulse union evaluated once per scanned row.
        from_keys = [f"a{position}" for position in range(0, 40, 2)]
        to_keys = [f"b{position}" for position in range(1, 40, 2)]
        rows = handle.execute(
            "MATCH (a:A)-[r:R]->(b:B) WHERE (a.id IN $x OR b.id IN $y) "
            "RETURN a.id, b.id, r.confidence LIMIT 5000",
            {"x": from_keys, "y": to_keys},
        ).rows
        expected = {
            (f"a{position}", f"b{(position * 5) % 12}")
            for position in range(12)
            if position % 2 == 0 or ((position * 5) % 12) % 2 == 1
        }
        assert {(row[0], row[1]) for row in rows} == expected
        assert walks == []
    finally:
        handle.close()


# --- door level: build, decline, staleness, ceiling, exact parity ----------------------------


def test_build_accepts_only_detached_string_bytes_null_tuples() -> None:
    context = _context()
    memo = _build_in_list_memo(("a", b"b", None, "a"), context)
    assert memo is not None
    assert memo.has_null is True
    assert len(memo.keys) == 2
    assert context.in_list_memo_elements == 4
    assert _build_in_list_memo(["a"], _context()) is None
    assert _build_in_list_memo((1, 1.0, "1"), _context()) is None
    assert _build_in_list_memo((True,), _context()) is None
    assert _build_in_list_memo(("a", bytearray(b"b")), _context()) is None
    assert _build_in_list_memo(("a", ("b",)), _context()) is None
    empty = _build_in_list_memo((), _context())
    assert empty is not None and empty.has_null is False and not empty.keys


def test_memo_is_keyed_by_name_and_proved_by_object_identity() -> None:
    context = _context()
    first = tuple(["a", "b"])
    memo = _in_list_memo("ids", first, context)
    assert memo is not None and _in_list_memo("ids", first, context) is memo
    assert _in_list_memo("ids", tuple(["a", "b"]), context) is None
    declined = _context()
    assert _in_list_memo("ns", [1], declined) is None
    assert declined.in_list_memos == {"ns": None}
    assert _in_list_memo("ns", (1,), declined) is None


def test_ceiling_counts_every_element_of_the_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_module, "_IN_LIST_MEMO_MAX_TOTAL_ELEMENTS", 5)
    context = _context()
    assert _build_in_list_memo(("a", "b", "c"), context) is not None
    assert _build_in_list_memo(("d", "e", "f"), context) is None
    assert _build_in_list_memo(("d", "e"), context) is not None
    assert context.in_list_memo_elements == 5
    assert _build_in_list_memo((), context) is not None


@pytest.mark.parametrize(
    "left",
    [
        None,
        "a",
        "zz",
        "",
        b"b",
        bytearray(b"b"),
        1,
        1.0,
        True,
        "1",
        ["a"],
        ("a",),
        {"k": "a"},
        math.nan,
    ],
)
@pytest.mark.parametrize(
    "values",
    [("a", b"b"), ("a", None), (), (None,), ("", "a", "a"), (b"b", None)],
)
def test_memo_membership_matches_the_walk(
    left: object, values: tuple[object, ...]
) -> None:
    memo = _build_in_list_memo(values, _context())
    assert memo is not None
    assert _memo_membership(left, memo) is _membership(left, values)


def test_unhashable_left_falls_back_to_the_walk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Opaque:
        __hash__ = None  # type: ignore[assignment]

    walks: list[tuple[object, object]] = []
    original = engine_module._membership

    def counting(left: object, right: object) -> object:
        walks.append((left, right))
        return original(left, right)

    monkeypatch.setattr(engine_module, "_membership", counting)
    values = ("a", None)
    memo = _build_in_list_memo(values, _context())
    assert memo is not None
    left = Opaque()
    assert _memo_membership(left, memo) is original(left, values) is None
    assert walks == [(left, values)]
    plain = _build_in_list_memo(("a",), _context())
    assert plain is not None
    assert _memo_membership(left, plain) is False
    assert len(walks) == 2


def test_walk_stops_at_the_first_match(monkeypatch: pytest.MonkeyPatch) -> None:
    compared: list[tuple[object, object]] = []
    original = engine_module._equal

    def counting(left: object, right: object) -> bool:
        compared.append((left, right))
        return original(left, right)

    monkeypatch.setattr(engine_module, "_equal", counting)
    assert _membership("a", ("a", "b", "c", None)) is True
    assert compared == [("a", "a")]
    compared.clear()
    assert _membership("zz", ("a", None, "b")) is None
    assert compared == [("zz", "a"), ("zz", "b")]
    compared.clear()
    assert _membership("zz", ("a", "b")) is False
    assert len(compared) == 2


def test_equal_fast_path_keeps_the_value_matrix() -> None:
    assert _equal("a", "a") is True
    # Two distinct objects spelling one value: the fast path compares payloads, not identity.
    assert _equal("".join(["a", "b"]), "".join(["a", "b"])) is True
    assert _equal(bytes([97]), bytes([97])) is True
    assert _equal("a", "b") is False
    assert _equal("", "") is True
    assert _equal(b"a", b"a") is True
    assert _equal(b"a", b"b") is False
    assert _equal(b"a", bytearray(b"a")) is True
    assert _equal(bytearray(b"a"), b"a") is True
    assert _equal("1", 1) is False
    assert _equal(1, "1") is False
    assert _equal(1, 1.0) is True
    assert _equal(True, 1) is False
    assert _equal(True, True) is True
    assert _equal("a", b"a") is False
    assert _equal(["a"], ("a",)) is True
    assert _equal({"k": 1}, {"k": 1.0}) is False
