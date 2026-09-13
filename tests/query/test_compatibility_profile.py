"""New language policies and the pinned reference inventory, independent of Ladybug."""

from __future__ import annotations

from pathlib import Path

import pytest
import okto_grafx

from okto_grafx.domain.errors import GrafxPlanError
from tests.query.stack import build_query_stack
from tools.check_opencypher import compile_feature


@pytest.mark.parametrize(
    ("query", "parameters", "expected"),
    (
        ("RETURN [1, 2, 3][0] AS value", {}, ((1,),)),
        ("RETURN [[1]][0][0] AS value", {}, ((1,),)),
        ("WITH $expr AS expr, $idx AS idx RETURN expr[idx] AS value",
         {"expr": ["Apa"], "idx": 0}, (("Apa",),)),
        ("RETURN [1, 2][-1], [1, 2][-2], [1, 2][-3], [1, 2][2]", {},
         ((2, 1, None, None),)),
        ("RETURN [][0], [][-1], [1][null], null[0]", {}, ((None, None, None, None),)),
        ("RETURN CASE null WHEN null THEN 1 ELSE 2 END", {}, ((2,),)),
        ("RETURN CASE WHEN true THEN 1 ELSE 1 / 0 END", {}, ((1,),)),
        ("RETURN CASE 1 WHEN 1 THEN 2 WHEN 1 / 0 THEN 3 END", {}, ((2,),)),
        ("RETURN {a: 1, A: 2}.a, {a: 1, A: 2}.A, {a: 1}.missing", {}, ((1, 2, None),)),
        ("RETURN $m[$key], $m.missing", {"m": {"a": 1, "A": 2}, "key": "A"}, ((2, None),)),
        ("RETURN {a: [1, 2]}['a'][0]", {}, ((1,),)),
        ("RETURN [null] = [null], [null, 1] = [null, 2], [1] = [1.0]", {}, ((None, False, True),)),
        ("RETURN {a: null} = {a: null}, {a: 1} = {b: 1}", {}, ((None, False),)),
        ("RETURN null IN [], [null] IN [[null]], [1] IN [[1.0]]", {}, ((False, None, True),)),
        ("RETURN 9007199254740992 = 9007199254740993, 9007199254740992 IN $xs",
         {"xs": (9007199254740993,)}, ((False, False),)),
        ("RETURN coalesce(7, 1 / 0)", {}, ((7,),)),
        ("RETURN coalesce([1], {a: 2}), coalesce(1, 'other'), coalesce(1, 2.5)",
         {}, (((1,), 1, 1),)),
        ("RETURN CASE WHEN true THEN [1] ELSE {a: 2} END", {}, (((1,),),)),
        ("RETURN CASE [1] WHEN [1] THEN 'yes' ELSE 2 END", {}, (("yes",),)),
        ("WITH DISTINCT 1 AS n RETURN n", {}, ((1,),)),
        ("WITH 1 AS n LIMIT 0 RETURN n", {}, ()),
        ("UNWIND [1, 2] AS x WITH x UNWIND [3, 4] AS y RETURN x, y", {},
         ((1, 3), (1, 4), (2, 3), (2, 4))),
        ("WITH [1, 2] AS xs UNWIND xs AS x RETURN x", {}, ((1,), (2,))),
        ("WITH [1] AS x WITH x[0] AS x RETURN x", {}, ((1,),)),
        ("WITH 1 AS x, 2 AS y WITH y AS x, x AS y RETURN x, y", {}, ((2, 1),)),
        ("WITH ['old'] AS x WITH x[0] AS y WITH y, {a: 7} AS x RETURN y, x.a",
         {}, (("old", 7),)),
        ("WITH 1 AS x WITH 2 AS x RETURN x ORDER BY x", {}, ((2,),)),
        ("WITH 1 AS x, 99 AS `\x00scope1` WITH 2 AS x, `\x00scope1` AS y RETURN x, y", {}, ((2, 99),)),
        ("WITH split('spec::fr', ':') AS p RETURN p[0], p[1], p[2]",
         {}, (("spec", "", "fr"),)),
        ("RETURN 1 AS x UNION RETURN 2 AS x UNION RETURN 1 AS x", {}, ((1,), (2,))),
        ("RETURN 1 AS x UNION ALL RETURN 1 AS x UNION ALL RETURN 'text' AS x",
         {}, ((1,), (1,), ("text",))),
        ("CALL () { RETURN 1 AS x UNION RETURN 1 AS x } RETURN x UNION ALL RETURN 1 AS x", {}, ((1,), (1,))),
        ("CALL () { RETURN 1 AS x UNION ALL RETURN 1 AS x } RETURN x UNION RETURN 1 AS x", {}, ((1,),)),
        ("WITH 1 AS n WITH 2 AS n RETURN n AS x UNION RETURN 3 AS x", {}, ((2,), (3,))),
        ("CALL { RETURN 1 AS x } RETURN x", {}, ((1,),)),
        ("UNWIND [1, 2] AS n CALL (n) { RETURN n + 1 AS x } RETURN n, x",
         {}, ((1, 2), (2, 3))),
        ("WITH 1 AS n WITH 2 AS n CALL (n) { WITH n RETURN n + 1 AS x } RETURN n, x",
         {}, ((2, 3),)),
        ("CALL { RETURN 1 AS x UNION RETURN 2 AS x UNION RETURN 3 AS x } "
         "RETURN x ORDER BY x DESC LIMIT 2", {}, ((3,), (2,))),
        ("WITH 1 AS n CALL (n) { CALL (n) { RETURN n + 1 AS x } RETURN x + 1 AS y } RETURN y",
         {}, ((3,),)),
        ("WITH ['x'] AS xs CALL (xs) { RETURN xs[0] AS first } RETURN upper(first)", {}, (("X",),)),
        ("CALL { RETURN [1, 2] AS xs } UNWIND xs AS x RETURN x", {}, ((1,), (2,))),
        ("RETURN range(1, 5, 2), range(3, 1, -1), range(3, 1)", {},
         (((1, 3, 5), (3, 2, 1), ()),)),
        ("RETURN substring('abcde', 1, 2), left('abc', 2), right('abc', 0), replace('aba', 'a', 'xy')",
         {}, (("bc", "ab", "", "xybxy"),)),
        ("RETURN head([1, 2]), last([]), tail([1, 2]), reverse('ab'), reverse([1, 2]), keys({a: 1})",
         {}, ((1, None, (2,), "ba", (2, 1), ("a",)),)),
        ("RETURN toInteger('1.7'), toInteger('9007199254740993'), toInteger('not a number'), "
         "toFloat('2.5'), toBoolean('true'), toString(false)", {},
         ((1, 9007199254740993, None, 2.5, True, "false"),)),
        ("RETURN ceil(1.2), floor(1.8), sign(-3), sqrt(4), round(1.5), sin(0)", {},
         ((2.0, 1.0, -1, 2.0, 2.0, 0.0),)),
        ("RETURN 1 AS x UNION RETURN 1.0 AS x UNION RETURN true AS x", {}, ((1,), (True,))),
    ),
)
def test_fixed_reference_expression_results(
    query: str, parameters: dict[str, object], expected: tuple[tuple[object, ...], ...],
) -> None:
    """Check zero-based access, null extension and conditional evaluation on real operators."""
    stack = build_query_stack()
    result = stack.engine.execute(query, stack.transaction(read_lsn=1000), parameters)
    assert result.rows == expected
    with okto_grafx.connect(":memory:") as database:
        assert database.execute(query, parameters).rows == expected


@pytest.mark.parametrize("index", (True, 1.0, "0"))
def test_non_integer_indices_still_refuse_before_empty_reads(index: object) -> None:
    """Out-of-range null results do not permit invalid index types."""
    stack = build_query_stack()
    with pytest.raises(GrafxPlanError):
        stack.engine.execute("MATCH (p:Person) RETURN $items[$index]",
                             stack.transaction(read_lsn=1000), {"items": [1], "index": index})


def test_tck_inventory_expands_examples_and_keeps_background_steps() -> None:
    """Count expanded scenarios and preserve all setup, without claiming execution passes."""
    source = '''Feature: Inventory regression
  Background:
    Given an empty graph
  Scenario Outline: select <number>
    When executing query:
      """
      RETURN <number> AS n
      """
    Then the result should be, in any order:
      | n |
      | <number> |
    Examples:
      | number |
      | 1 |
      | 2 |
'''
    cases = compile_feature(source, "expressions/example.feature")
    assert len(cases) == 2
    assert [case["id"] for case in cases] == [
        "expressions/example.feature#0001", "expressions/example.feature#0002"]
    for ordinal, case in enumerate(cases, start=1):
        assert case["conformance"] == "not_run"
        assert case["steps"][0]["text"] == "an empty graph"
        assert case["queries"][0]["query"] == f"RETURN {ordinal} AS n"
        assert case["queries"][0]["parse_status"] == "accepted"


def test_tck_inventory_preserves_negative_scenarios() -> None:
    """Unsupported syntax is an observed refusal, not a discarded scenario or TCK pass."""
    cases = compile_feature('''Feature: negative
  Scenario: refusal
    When executing query:
      """
      RETURN (
      """
    Then an error should be raised
''', "negative.feature")
    assert len(cases) == 1
    assert cases[0]["queries"][0]["parse_status"] == "refused"
    assert cases[0]["conformance"] == "not_run"


def test_with_window_and_distinct_before_a_second_projection() -> None:
    """WITH uses shared operators for duplicate removal, ordering and row windows."""
    stack = build_query_stack()
    for identity, name in enumerate(("b", "a", "b", "c"), start=1):
        stack.insert("Person", identity, (identity, name, 20, None), csn=1)
    result = stack.engine.execute(
        "MATCH (p:Person) WITH DISTINCT p.name AS name ORDER BY name SKIP $skip LIMIT $limit "
        "WITH name, size(name) AS length RETURN name, length",
        stack.transaction(read_lsn=1000), {"skip": 1, "limit": 1},
    )
    assert result.rows == (("b", 1),)


def test_match_after_with_keeps_the_projection_and_its_window() -> None:
    """A later MATCH reads rows from the stage before it, not a flattened set of scans."""
    stack = build_query_stack()
    for identity in (1, 2, 3):
        stack.insert("Person", identity, (identity, str(identity), 20, None), csn=1)
    result = stack.engine.execute(
        "MATCH (p:Person) WITH p.id AS chosen ORDER BY chosen LIMIT 1 "
        "MATCH (q:Person) WHERE q.id = chosen RETURN q.id",
        stack.transaction(read_lsn=1000), {},
    )
    assert result.rows == ((1,),)


def test_write_with_read_pipeline_is_atomic_and_reads_own_inserts(tmp_path: Path) -> None:
    """Clause composition uses the same transaction for every write and subsequent read."""
    with okto_grafx.connect(tmp_path / "pipeline", page_size=512) as db:
        with db.begin("write") as schema:
            schema.execute("CREATE NODE TABLE T(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            result = tx.execute(
                "UNWIND [1, 2] AS x CREATE (:T {id: x}) "
                "WITH count(*) AS created MATCH (n:T) RETURN created, n.id ORDER BY n.id"
            )
            assert result.rows == ((2, 1), (2, 2))
        assert db.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (2,))
        with db.begin("write") as tx:
            with pytest.raises(GrafxPlanError):
                tx.execute("CREATE (:T {id: 3}) WITH 1 AS x RETURN x / 0")
        assert db.execute("MATCH (n:T) RETURN n.id ORDER BY n.id").rows == ((1,), (2,))


@pytest.mark.parametrize(
    ("query", "expected"),
    (
        ("MATCH (a:Person) OPTIONAL MATCH (b:Person) WHERE b.id = a.id AND b.id > 1 "
         "RETURN a.id, b.id ORDER BY a.id", ((1, None), (2, 2), (3, 3))),
        ("OPTIONAL MATCH (a:Person) WHERE a.id = 99 WITH a RETURN a.id", ((None,),)),
        ("MATCH (a:Person) OPTIONAL MATCH (b:Person), (c:Person) "
         "WHERE b.id = a.id AND c.id < b.id RETURN a.id, b.id, c.id ORDER BY a.id, c.id",
         ((1, None, None), (2, 2, 1), (3, 3, 1), (3, 3, 2))),
        ("MATCH (a:Person) OPTIONAL MATCH (b:Person) WHERE b.id = 99 "
         "MATCH (b) RETURN b.id", ()),
        ("MATCH (p:Person) WITH p AS person RETURN person.id ORDER BY person.id", ((1,), (2,), (3,))),
        ("MATCH (p:Person) CALL (p) { RETURN p AS q } MATCH (q:Person) RETURN q.id ORDER BY q.id",
         ((1,), (2,), (3,))),
        ("MATCH (p:Person) CALL (p) { MATCH (q:Person) WHERE q.id < p.id RETURN count(*) AS total } "
         "RETURN p.id, total ORDER BY p.id", ((1, 0), (2, 1), (3, 2))),
    ),
)
def test_generic_optional_correlation_and_null_extension(
    query: str, expected: tuple[tuple[object, ...], ...],
) -> None:
    """Optional extends after all inner patterns/WHERE, preserving each outer row's identity."""
    stack = build_query_stack()
    for identity in (1, 2, 3):
        stack.insert("Person", identity, (identity, str(identity), 20, None), csn=1)
    result = stack.engine.execute(query, stack.transaction(read_lsn=1000), {})
    assert result.rows == expected


def test_connected_node_delete_requires_detach_or_explicit_edge_delete(tmp_path: Path) -> None:
    """A caught statement error cannot commit a node deletion that leaves live edges."""
    from okto_grafx.domain.errors import GrafxQueryError
    with okto_grafx.connect(tmp_path / "delete", page_size=512) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE T(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM T TO T)")
        with db.begin("write") as tx:
            tx.execute("CREATE (:T {id: 1}), (:T {id: 2})")
        with db.begin("write") as tx:
            tx.execute("MATCH (a:T), (b:T) WHERE a.id = 1 AND b.id = 2 CREATE (a)-[:R]->(b)")
        with db.begin("write") as tx:
            with pytest.raises(GrafxQueryError, match="live relationships"):
                tx.execute("MATCH (n:T) WHERE n.id = 1 DELETE n")
        assert db.execute("MATCH (n:T) RETURN count(*)").rows == ((2,),)
        with db.begin("write") as tx:
            tx.execute("MATCH (a:T)-[r:R]->(b:T) DELETE a, r")
        assert db.execute("MATCH (n:T) RETURN n.id").rows == ((2,),)


def test_result_detachment_failure_leaves_no_statement_writes(tmp_path: Path, monkeypatch) -> None:
    """Output serialization is part of statement atomicity, not a post-staging best effort."""
    import okto_grafx.engine.query_engine as engine
    with okto_grafx.connect(tmp_path / "projection", page_size=512) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE T(id INT64, PRIMARY KEY(id))")
        with db.begin("write") as tx:
            with monkeypatch.context() as scoped:
                def refuse(*args):
                    raise GrafxPlanError("forced result detachment failure")
                scoped.setattr(engine, "_projected", refuse)
                with pytest.raises(GrafxPlanError, match="forced result"):
                    tx.execute("CREATE (n:T {id: 1}) RETURN n.id")
        assert db.execute("MATCH (n:T) RETURN count(*)").rows == ((0,),)


def test_tck_runner_preserves_wrong_results_and_unknown_fixtures():
    from tools.check_opencypher import run_read_case
    from types import SimpleNamespace
    calls = []
    class Executor:
        def execute(self, text, parameters):
            calls.append(text)
            return SimpleNamespace(columns=("v",), rows=((2,),))
    case = {"steps": [
        {"text": "an empty graph"},
        {"text": "executing query:", "argument": {"docString": {"content": "RETURN 1 AS v"}}},
        {"text": "the result should be, in order:", "argument": {"dataTable": {"rows": [
            {"cells": [{"value": "v"}]}, {"cells": [{"value": "1"}]},
        ]}}},
        {"text": "no side effects"},
    ]}
    assert run_read_case(case, Executor())["conformance"] == "failed"
    assert len(calls) == 1
    case["steps"].append({"text": "an unimplemented fixture"})
    assert run_read_case(case, Executor())["conformance"] == "not_run"
    assert len(calls) == 1


@pytest.mark.parametrize("revision,dirty", [("wrong", ""),
    ("677cbafabb8c3c5eed458fd3b1ec0daec8d67d23", " M tck/features/example.feature")])
def test_tck_inventory_refuses_a_different_or_modified_reference(tmp_path, monkeypatch, revision, dirty):
    from tools import check_opencypher
    from types import SimpleNamespace
    def result(command, **kwargs):
        return SimpleNamespace(stdout=revision if "rev-parse" in command else dirty)
    monkeypatch.setattr(check_opencypher.subprocess, "run", result)
    with pytest.raises(ValueError):
        check_opencypher.inventory(tmp_path)
