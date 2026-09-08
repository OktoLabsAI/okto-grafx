"""EXEC-CSE: a WHERE predicate compiled to closures answers exactly as the canonical walk."""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

import okto_grafx
import okto_grafx.engine.query_engine as query_engine_module
from okto_grafx.domain.errors import GrafxError
from okto_grafx.domain.query.ast import (
    BinaryOperation,
    Expression,
    FunctionCall,
    ListExpression,
    Literal,
    NullCheck,
    Parameter,
    Property,
    UnaryOperation,
    Variable,
)
from okto_grafx.domain.query.plan import FilterRows
from okto_grafx.engine.query_engine import (
    _COMPILED_MISS,
    _COMPILED_PREDICATE_MAX_ENTRIES,
    _Row,
    _evaluate,
)

N = Variable(name="n")
M = Variable(name="m")
LIT = {True: Literal(value=True), False: Literal(value=False), None: Literal(value=None)}
BOOM = Property(subject=N, key="coluna_que_nao_existe")
PARAMS = {"escalar": 7, "mapa": {"k": 1}, "x": -1, "layer": "canonical"}


def P(key: str) -> Property:
    return Property(subject=N, key=key)


def CO(*arguments: object) -> FunctionCall:
    return FunctionCall(name="coalesce", arguments=tuple(arguments))  # type: ignore[arg-type]


def B(operator: str, left: object, right: object) -> BinaryOperation:
    return BinaryOperation(operator=operator, left=left, right=right)  # type: ignore[arg-type]


def corpus() -> list[tuple[str, object]]:
    """The hostile corpus of the L6 EXEC dimension, plus the CSE-specific attacks."""
    out: list[tuple[str, object]] = []
    operands = {
        "T": LIT[True],
        "F": LIT[False],
        "N": LIT[None],
        "i5": Literal(value=5),
        "s": Literal(value="x"),
    }
    for operator in ("AND", "OR", "XOR"):
        for a, ea in operands.items():
            for b, eb in operands.items():
                out.append((f"{operator}_{a}_{b}", B(operator, ea, eb)))
    for a, ea in operands.items():
        out.append((f"NOT_{a}", UnaryOperation(operator="NOT", operand=ea)))
        out.append((f"NEG_{a}", UnaryOperation(operator="-", operand=ea)))
    for operator in ("<", "<=", ">", ">=", "=", "<>"):
        out.append((f"null_col_{operator}", B(operator, P("revocation_reason"), Literal(value="a"))))
        out.append((f"col_{operator}_null", B(operator, P("title"), LIT[None])))
    pairs = [
        ("str_int", P("title"), Literal(value=5)),
        ("int_str", P("created_at"), Literal(value="a")),
        ("bool_int", P("p14"), Literal(value=True)),
        ("int_bool", Literal(value=1), P("p16")),
        ("true_eq_1", LIT[True], Literal(value=1)),
        ("int_eq_float", P("created_at"), Literal(value=1.0)),
        ("double_ge_int", P("source_confidence"), Literal(value=0)),
        ("str_ge_str", P("title"), Literal(value="conteudo")),
        ("param_eq_col", Parameter(name="layer"), P("graph_layer")),
    ]
    for name, left, right in pairs:
        for operator in ("<", ">=", "=", "<>"):
            out.append((f"{name}_{operator}", B(operator, left, right)))
    for key in ("revocation_reason", "title", "coluna_que_nao_existe"):
        out.append((f"isnull_{key}", NullCheck(operand=P(key), negated=False)))
        out.append((f"isnotnull_{key}", NullCheck(operand=P(key), negated=True)))
    out.append(("coluna_inexistente", B(">=", BOOM, Literal(value=1))))
    out.append(("variavel_nao_ligada", B(">=", Property(subject=M, key="id"), Literal(value=1))))
    out.append(("parametro_ausente", B(">=", P("created_at"), Parameter(name="nao_existe"))))
    out.append(("sujeito_nao_binding", Property(subject=Parameter(name="escalar"), key="k")))
    out.append(("sujeito_mapping", Property(subject=Parameter(name="mapa"), key="k")))
    out.append(("sujeito_mapping_ausente", Property(subject=Parameter(name="mapa"), key="zzz")))
    out.append(("curto_and_falso", B("AND", LIT[False], B(">=", BOOM, Literal(value=1)))))
    out.append(("curto_or_verdadeiro", B("OR", LIT[True], B(">=", BOOM, Literal(value=1)))))
    out.append(("curto_and_nulo_direita_erra", B("AND", LIT[None], B(">=", BOOM, Literal(value=1)))))
    out.append(
        (
            "ordem_de_erro_esquerda_primeiro",
            B(
                "AND",
                B(">=", BOOM, Literal(value=1)),
                B(">=", Property(subject=M, key="id"), Literal(value=1)),
            ),
        )
    )
    out.append(
        (
            "coalesce_null_str",
            B("<>", CO(P("revocation_reason"), Literal(value="")), Literal(value="source_deleted")),
        )
    )
    out.append(("coalesce_todos_nulos", CO(P("revocation_reason"), LIT[None])))
    out.append(("coalesce_familias_misturadas", CO(P("title"), Literal(value=5))))
    out.append(("coalesce_int_float", CO(P("created_at"), Literal(value=1.5))))
    out.append(("coalesce_float_int", CO(P("source_confidence"), Literal(value=1))))
    out.append(("coalesce_bool", CO(P("p14"), LIT[True])))
    out.append(("coalesce_um_arg", CO(P("revocation_reason"))))
    out.append(("coalesce_lista", CO(ListExpression(elements=(LIT[True],)), LIT[True])))
    out.append(
        (
            "coalesce_aninhado",
            B("=", CO(CO(P("revocation_reason"), LIT[None]), Literal(value="x")), Literal(value="x")),
        )
    )
    out.append(
        (
            "in_lista",
            B("IN", P("graph_layer"), ListExpression(elements=(Literal(value="canonical"), LIT[None]))),
        )
    )
    out.append(("starts_with", B("STARTS WITH", P("title"), Literal(value="t"))))
    out.append(("arith", B(">", B("+", P("created_at"), Literal(value=1)), Literal(value=3))))
    # --- CSE attacks: the same subtree repeated, first reached on a branch that may be skipped.
    tomb = B("<>", CO(P("revocation_reason"), Literal(value="")), Literal(value="r"))
    out.append(("cse_twice", B("AND", tomb, B("<>", CO(P("revocation_reason"), Literal(value="")), Literal(value="q")))))
    seven = None
    for index in range(7):
        term = B("<>", CO(P("revocation_reason"), Literal(value="")), Literal(value=f"reason-{index}"))
        seven = term if seven is None else B("AND", seven, term)
    out.append(("cse_seven", seven))
    boom = B(">=", BOOM, Literal(value=1))
    out.append(("cse_first_in_skipped_branch", B("OR", B("AND", LIT[False], boom), boom)))
    out.append(("cse_raises_once", B("AND", boom, boom)))
    out.append(("cse_first_in_null_and", B("AND", B("AND", LIT[None], boom), boom)))
    shared_null = P("revocation_reason")
    out.append(("cse_null_property", B("AND", NullCheck(operand=shared_null), NullCheck(operand=shared_null, negated=True))))
    out.append(("cse_key_collision", B("AND", B("=", P("p16"), Literal(value=1)), B("=", P("p16"), LIT[True]))))
    out.append(("cse_zero_collision", B("OR", B("=", P("source_confidence"), Literal(value=0.0)), B("=", P("source_confidence"), Literal(value=-0.0)))))
    coalesce_double = CO(P("source_confidence"), Literal(value=1))
    out.append(("cse_coalesce_double", B("=", coalesce_double, CO(P("source_confidence"), Literal(value=1)))))
    return out


@pytest.fixture
def database(tmp_path: Path) -> Iterator[object]:
    handle = okto_grafx.connect(tmp_path / "db", page_size=4096)
    with handle.begin("write") as transaction:
        transaction.execute(
            "CREATE NODE TABLE Node(id STRING, title STRING, created_at INT64, "
            "revocation_reason STRING, graph_layer STRING, p14 BOOL, p16 INT64, "
            "source_confidence DOUBLE, PRIMARY KEY(id))"
        )
    with handle.begin("write") as transaction:
        rows = [
            ("n-0", "title zero", 0, None, "canonical", True, 1, 0.0),
            ("n-1", "conteudo", 1, "source_deleted", "shadow", False, 0, 0.5),
            ("n-2", "t2", 2, "", "canonical", None, None, None),
            ("n-3", None, 3, "x", "canonical", True, 7, -0.0),
        ]
        for values in rows:
            transaction.execute(
                "CREATE (:Node {id: $id, title: $title, created_at: $created_at, "
                "revocation_reason: $rr, graph_layer: $gl, p14: $p14, p16: $p16, "
                "source_confidence: $sc})",
                dict(
                    zip(
                        ("id", "title", "created_at", "rr", "gl", "p14", "p16", "sc"),
                        values,
                    )
                ),
            )
    try:
        yield handle
    finally:
        handle.close()


def _capture(database: object, monkeypatch: pytest.MonkeyPatch) -> tuple[list[_Row], object]:
    """Capture the real rows and statement context of one filtered scan."""
    captured: dict[str, object] = {}
    original = query_engine_module._filter_rows

    def grab(engine, node, context):
        if "rows" not in captured:
            captured["context"] = context
            rows = []
            for row in engine._rows(node.child, context):
                rows.append(row)
                yield row
            captured["rows"] = rows
            return
        yield from original(engine, node, context)

    monkeypatch.setattr(query_engine_module, "_filter_rows", grab)
    for kind, handler in list(query_engine_module._HANDLERS.items()):
        if handler is original:
            monkeypatch.setitem(query_engine_module._HANDLERS, kind, grab)
    # The captured binding is intentionally reused below against every column, outside the
    # statement that produced it. Ask that statement for the complete stored shape so this
    # white-box harness does not retain an internal projected-row proof past its closed plan.
    database.execute(
        "MATCH (n:Node) WHERE n.created_at >= $x "
        "RETURN n.id, n.title, n.created_at, n.revocation_reason, n.graph_layer, "
        "n.p14, n.p16, n.source_confidence",
        dict(PARAMS),
    )
    context = captured["context"]
    context.parameters.update(PARAMS)  # type: ignore[attr-defined]
    return captured["rows"], context  # type: ignore[return-value]


def _snapshot(function, row: _Row, context: object) -> tuple:
    try:
        value = function(row, context)
    except GrafxError as failure:
        return ("erro", json.dumps(failure.to_dict(), sort_keys=True, default=repr))
    except Exception as failure:  # noqa: BLE001 - the shape of a Python error is the fixture
        return ("erro_py", type(failure).__name__, str(failure))
    return ("valor", type(value).__name__, repr(value))


def _compiled(engine, expression, context):
    compiled = engine._compiled_predicate(expression, context)

    def run(row: _Row, ctx: object) -> object:
        memo = [_COMPILED_MISS] * compiled.slots + [0] if compiled.slots else None
        return compiled.function(row, ctx, memo)

    return run


def test_every_hostile_case_answers_exactly_as_the_canonical_walk(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, context = _capture(database, monkeypatch)
    assert len(rows) == 4
    engine = database._queries  # type: ignore[attr-defined]
    binding = rows[0].bindings["n"]
    short = replace(binding.version, values=binding.version.values[:3])
    subjects = [(f"row-{index}", row) for index, row in enumerate(rows)]
    subjects.append(("tupla_curta", _Row(bindings={"n": replace(binding, version=short)})))
    comparisons = 0
    mismatches = []
    for name, expression in corpus():
        compiled = _compiled(engine, expression, context)
        for label, row in subjects:
            expected = _snapshot(lambda r, c: _evaluate(expression, r, c), row, context)
            observed = _snapshot(compiled, row, context)
            comparisons += 1
            if observed != expected:
                mismatches.append((name, label, expected, observed))
    assert comparisons >= 600
    assert mismatches == []


def test_a_computed_row_takes_the_canonical_walk_not_the_closures(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, context = _capture(database, monkeypatch)
    computed_row = _Row(bindings=dict(rows[0].bindings), computed={Literal(value=1): 1})
    # _evaluate consults the aggregation memo at every node: Literal(True) == Literal(1) there.
    expression = B("AND", LIT[True], LIT[True])
    assert _evaluate(expression, computed_row, context) is None
    assert query_engine_module._predicate_admits(expression, computed_row, context) is False
    plain_row = _Row(bindings=dict(rows[0].bindings))
    assert query_engine_module._predicate_admits(expression, plain_row, context) is True


def test_shared_subtrees_are_evaluated_once_per_row_and_only_when_reached(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, context = _capture(database, monkeypatch)
    engine = database._queries  # type: ignore[attr-defined]
    reads = 0
    original_value = query_engine_module.RowBinding.value

    def counted_value(self, key):
        nonlocal reads
        reads += 1
        return original_value(self, key)

    monkeypatch.setattr(query_engine_module.RowBinding, "value", counted_value)
    seven = None
    for index in range(7):
        term = B("<>", CO(P("revocation_reason"), Literal(value="")), Literal(value=f"reason-{index}"))
        seven = term if seven is None else B("AND", seven, term)
    compiled = engine._compiled_predicate(seven, context)
    assert compiled.slots >= 1
    run = _compiled(engine, seven, context)
    reads = 0
    assert run(rows[0], context) is True
    assert reads == 1, "seven identical coalesce terms read the property once per row"
    reads = 0
    _evaluate(seven, rows[0], context)
    assert reads == 7
    # A shared subtree first met on a skipped branch is evaluated where the walk meets it.
    boom = B(">=", BOOM, Literal(value=1))
    skipped = B("OR", B("AND", LIT[False], boom), B("AND", LIT[True], LIT[True]))
    assert _compiled(engine, skipped, context)(rows[0], context) is True


class _CountingMap(dict):
    """A hostile mapping parameter: every read is observable, and it can change its answer."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.reads = 0
        self.answers: list[object] = []

    def items(self):
        self.reads += 1
        if self.answers:
            return [(self.key, self.answers.pop(0))]
        return super().items()

    key = "k"


def test_a_mapping_subject_is_read_exactly_as_often_as_the_walk_reads_it(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review: no double evaluation of the subject, no CSE over an observable read."""
    rows, context = _capture(database, monkeypatch)
    engine = database._queries  # type: ignore[attr-defined]
    hostile = _CountingMap(k=1, inner={"k": 1})
    context.parameters["mapa"] = hostile  # type: ignore[attr-defined]
    single = Property(subject=Parameter(name="mapa"), key="k")
    # The subject itself is an observable read: evaluating it twice would double the count.
    nested = Property(subject=Property(subject=Parameter(name="mapa"), key="inner"), key="k")
    read_twice = B("AND", B("=", single, Literal(value=1)), B("=", single, Literal(value=1)))
    coalesced_twice = B(
        "AND",
        B("=", CO(single, Literal(value=0)), Literal(value=1)),
        B("=", CO(single, Literal(value=0)), Literal(value=1)),
    )
    # A repeated subtree that falls back to the canonical walk (arithmetic is not compiled)
    # around the mapping read: the walk does not count its reads, so it is never memoized.
    fallback_twice = B(
        "AND",
        B(">", B("+", single, Literal(value=1)), Literal(value=0)),
        B(">", B("+", single, Literal(value=1)), Literal(value=0)),
    )
    cases = (
        ("single", single),
        ("nested", nested),
        ("twice", read_twice),
        ("coalesce", coalesced_twice),
        ("fallback", fallback_twice),
    )
    for name, expression in cases:
        hostile.reads = 0
        expected = _snapshot(lambda r, c: _evaluate(expression, r, c), rows[0], context)
        walk_reads = hostile.reads
        hostile.reads = 0
        observed = _snapshot(_compiled(engine, expression, context), rows[0], context)
        assert observed == expected, name
        assert hostile.reads == walk_reads, (name, walk_reads, hostile.reads)
    # A mapping that changes its answer between reads: the walk sees 1 then 2 and answers False;
    # a compiled form that memoized the first read would answer True.
    hostile.answers = [1, 2]
    expected = _snapshot(lambda r, c: _evaluate(read_twice, r, c), rows[0], context)
    hostile.answers = [1, 2]
    observed = _snapshot(_compiled(engine, read_twice, context), rows[0], context)
    assert observed == expected == ("valor", "bool", "False")
    # A subject that changes between reads: the walk reads it once and sees k = 1.
    hostile.key = "inner"
    hostile.answers = [{"k": 1}, {"k": 2}]
    expected = _snapshot(lambda r, c: _evaluate(nested, r, c), rows[0], context)
    hostile.answers = [{"k": 1}, {"k": 2}]
    observed = _snapshot(_compiled(engine, nested, context), rows[0], context)
    assert observed == expected == ("valor", "int", "1")
    hostile.key = "k"
    # The same mutation through the canonical fallback: 1 + 1 > 0 then -5 + 1 > 0 is False.
    hostile.answers = [1, -5]
    expected = _snapshot(lambda r, c: _evaluate(fallback_twice, r, c), rows[0], context)
    hostile.answers = [1, -5]
    observed = _snapshot(_compiled(engine, fallback_twice, context), rows[0], context)
    assert observed == expected == ("valor", "bool", "False")
    # And the row-bound property beside it still shares its slot: only the mapping read is
    # excluded from memoization, not the whole predicate.
    mixed = B(
        "AND",
        B("=", CO(P("revocation_reason"), Literal(value="")), Literal(value="")),
        B("AND", B("=", single, Literal(value=1)), B("=", CO(P("revocation_reason"), Literal(value="")), Literal(value=""))),
    )
    reads = 0
    original_value = query_engine_module.RowBinding.value

    def counted_value(self, key):
        nonlocal reads
        reads += 1
        return original_value(self, key)

    monkeypatch.setattr(query_engine_module.RowBinding, "value", counted_value)
    hostile.answers = []
    hostile.reads = 0
    assert _compiled(engine, mixed, context)(rows[0], context) is True
    assert reads == 1 and hostile.reads == 1


def test_concurrent_readers_keep_the_bound_and_share_one_compiled_form(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review: the cache is published under a guard, so the hard bound holds and no
    concurrent eviction can surface a KeyError; the guard never wraps compilation."""
    _rows, context = _capture(database, monkeypatch)
    engine = database._queries  # type: ignore[attr-defined]
    original_compile = query_engine_module._compile_predicate
    compiles = 0
    compile_lock = threading.Lock()

    def slow_compile(expression, ctx):
        nonlocal compiles
        with compile_lock:
            compiles += 1
        for _ in range(200):  # widen the window in which two threads compile the same plan
            pass
        return original_compile(expression, ctx)

    monkeypatch.setattr(query_engine_module, "_compile_predicate", slow_compile)
    limit = query_engine_module._COMPILED_PREDICATE_MAX_ENTRIES
    workers = 8
    expressions = [
        B("=", P("revocation_reason"), Literal(value=f"v{index}")) for index in range(limit * 3)
    ]
    shared = B("=", P("revocation_reason"), Literal(value="shared"))
    interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # interleave the readers between every few bytecodes
    barrier = threading.Barrier(workers)
    flood = threading.Barrier(workers)  # every thread holds its shared form before any eviction
    failures: list[BaseException] = []
    shared_forms: list[object] = []
    peak = 0
    peak_lock = threading.Lock()

    def reader(offset: int) -> None:
        nonlocal peak
        try:
            barrier.wait()
            shared_forms.append(engine._compiled_predicate(shared, context))
            flood.wait()
            for index in range(offset, len(expressions), workers):
                engine._compiled_predicate(expressions[index], context)
                size = len(engine._compiled_predicates)
                with peak_lock:
                    peak = max(peak, size)
                if index % 7 == 0:
                    engine._compiled_predicate(shared, context)
        except BaseException as failure:  # noqa: BLE001 - every failure must reach the assert
            failures.append(failure)

    threads = [threading.Thread(target=reader, args=(offset,)) for offset in range(workers)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(interval)
    assert failures == []
    assert peak <= limit
    assert len(engine._compiled_predicates) <= limit
    assert len(shared_forms) == workers
    assert all(form is shared_forms[0] for form in shared_forms)
    assert compiles >= len(expressions) + 1
    assert not engine._compiled_predicates_lock.locked()


def test_the_compiled_cache_is_bounded_and_keyed_by_expression_identity(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows, context = _capture(database, monkeypatch)
    engine = database._queries  # type: ignore[attr-defined]
    cache = engine._compiled_predicates
    cache.clear()
    first = B("=", P("title"), Literal(value="t2"))
    twin = B("=", P("title"), Literal(value="t2"))
    assert first == twin and first is not twin
    entry = engine._compiled_predicate(first, context)
    assert engine._compiled_predicate(first, context) is entry
    assert engine._compiled_predicate(twin, context) is not entry
    for index in range(_COMPILED_PREDICATE_MAX_ENTRIES + 10):
        engine._compiled_predicate(B("=", P("created_at"), Literal(value=index)), context)
    assert len(cache) <= _COMPILED_PREDICATE_MAX_ENTRIES
    for compiled in cache.values():
        assert compiled.__slots__ == ("expression", "function", "slots", "calls")


def test_filter_and_vector_style_admission_agree_with_the_walk_end_to_end(database: object) -> None:
    rows = database.execute(
        "MATCH (n:Node) WHERE (coalesce(n.revocation_reason, '') <> 'source_deleted' "
        "AND coalesce(n.revocation_reason, '') <> 'x') AND n.p14 IS NOT NULL "
        "RETURN n.id ORDER BY n.id"
    ).rows
    assert rows == (("n-0",),)
    with pytest.raises(GrafxError) as refused:
        database.execute("MATCH (n:Node) WHERE n.created_at RETURN n.id")
    assert refused.value.details["field"] == "predicate"
    with pytest.raises(GrafxError) as missing:
        database.execute("MATCH (n:Node) WHERE n.nope = 1 RETURN n.id")
    assert missing.value.details["field"] == "column"


class _HostileRepr:
    """A literal value whose repr must never be asked for while keying."""

    def __repr__(self) -> str:
        raise AssertionError("repr() was called on a literal while compiling")


class _HostileNode(Expression):
    """An expression the compiler does not know; its children must never be walked."""

    def children(self) -> tuple[Expression, ...]:
        raise AssertionError("children() was walked on a fallback node while compiling")


def test_keying_never_calls_into_a_literal_nor_walks_a_fallback_node(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review: the structural key is closed (no repr, no hashing of arbitrary values) and
    a node that will run through the canonical walk is not visited below."""
    rows, context = _capture(database, monkeypatch)
    engine = database._queries  # type: ignore[attr-defined]
    hostile_value = _HostileRepr()
    hostile_literal = Literal(value=hostile_value)
    expression = B(
        "AND",
        B("=", P("p16"), Literal(value=1)),
        B("OR", B("=", P("p16"), hostile_literal), _HostileNode()),
    )
    compiled = query_engine_module._compile_predicate(expression, context)  # must not raise
    plain = B(
        "AND",
        B("=", P("p16"), Literal(value=1)),
        B("OR", B("=", P("p16"), Literal(value=2)), _HostileNode()),
    )
    slots_of = lambda candidate: query_engine_module._compile_predicate(candidate, context).slots  # noqa: E731
    assert compiled.slots == slots_of(plain) == 1  # only the repeated property shares
    # The same object twice is keyed by identity and may share; two distinct objects never do.
    twice_same = B("AND", B("=", P("p16"), hostile_literal), B("=", P("p16"), hostile_literal))
    twice_distinct = B(
        "AND",
        B("=", P("p16"), Literal(value=_HostileRepr())),
        B("=", P("p16"), Literal(value=_HostileRepr())),
    )
    assert slots_of(twice_same) == slots_of(twice_distinct) + 1
    # Floats are keyed by their bits, exact safe scalars by value and type.
    same_zero = B("AND", B("=", P("p16"), Literal(value=0.0)), B("=", P("p16"), Literal(value=0.0)))
    signed_zero = B(
        "AND", B("=", P("p16"), Literal(value=0.0)), B("=", P("p16"), Literal(value=-0.0))
    )
    assert slots_of(same_zero) == slots_of(signed_zero) + 1
    for name, candidate in (("hostile", expression), ("same", twice_same)):
        for row in rows:
            expected = _snapshot(lambda r, c: _evaluate(candidate, r, c), row, context)
            observed = _snapshot(_compiled(engine, candidate, context), row, context)
            assert observed == expected, (name, row)


def test_a_child_without_rows_never_compiles_its_predicate(
    database: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex review: zero rows means zero compilation, zero keying and zero cache entries."""
    compiles = 0
    original = query_engine_module._compile_predicate

    def counted(expression, context):
        nonlocal compiles
        compiles += 1
        return original(expression, context)

    monkeypatch.setattr(query_engine_module, "_compile_predicate", counted)
    with database.begin("write") as transaction:  # type: ignore[attr-defined]
        transaction.execute("CREATE NODE TABLE Empty(id STRING, k INT64, PRIMARY KEY(id))")
    engine = database._queries  # type: ignore[attr-defined]
    before = len(engine._compiled_predicates)
    rows = database.execute("MATCH (e:Empty) WHERE e.k = 1 RETURN e.id").rows  # type: ignore[attr-defined]
    assert not rows
    assert compiles == 0
    assert len(engine._compiled_predicates) == before
    rows = database.execute("MATCH (n:Node) WHERE n.p16 = 1 RETURN n.id").rows  # type: ignore[attr-defined]
    assert [row[0] for row in rows] == ["n-0"]
    assert compiles == 1


def test_plan_filter_nodes_are_compiled_once_per_cached_plan(database: object) -> None:
    engine = database._queries  # type: ignore[attr-defined]
    engine._compiled_predicates.clear()
    statement = "MATCH (n:Node) WHERE n.created_at >= $x AND n.title IS NOT NULL RETURN n.id"
    for _ in range(3):
        database.execute(statement, {"x": 0})
    predicates = [entry.expression for entry in engine._compiled_predicates.values()]
    assert len(predicates) == 1
    assert isinstance(predicates[0], BinaryOperation)
    plan = database.planned(statement) if hasattr(database, "planned") else None
    if plan is not None:
        filters = [node for node in plan.plan.walk() if isinstance(node, FilterRows)]
        assert filters and filters[0].predicate == predicates[0]
