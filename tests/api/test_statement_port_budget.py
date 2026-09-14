"""Per-statement budgets at the ports a statement crosses, on disk.

Two budgets, both counted at the port and not by reading call sites:

* how many participant page-access sections one public statement opens, and
* how many times an ORDER BY expression is walked for its free names.

Both are per-statement constants. A row-proportional or duplicated count is the defect these
tests exist to catch, so each assertion names an exact number rather than a bound.
"""

from __future__ import annotations

import pytest

from okto_grafx import connect
from okto_grafx.engine.txn_manager import TransactionManager


def _schema(db) -> None:
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE T(pk INT64, v INT64, s STRING, PRIMARY KEY(pk))")
        tx.execute("CREATE REL TABLE R(FROM T TO T, w INT64)")


def _rows(db, count: int) -> None:
    with db.begin("write") as tx:
        for i in range(count):
            tx.execute(
                "CREATE (n:T {pk:$p, v:$v, s:$s}) RETURN n.pk",
                {"p": i, "v": (i * 7) % count, "s": f"s{i % 3}"},
            )


@pytest.fixture
def count_sections(monkeypatch):
    """Count entries into the participant page-access section at the manager port."""
    tally = {"n": 0}
    original = TransactionManager.page_access_section

    def counted(self, *args, **kwargs):
        tally["n"] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(TransactionManager, "page_access_section", counted)
    return tally


def test_write_statement_opens_two_page_access_sections(tmp_path, count_sections):
    """One to run the statement and stage it, one to settle after the public result.

    The staging mark is taken inside the section that runs the statement. A third section here
    means the mark re-acquired the participant section, the file lock and the stat that the
    running statement was already holding.
    """
    with connect(tmp_path / "write-budget") as db:
        _schema(db)
        _rows(db, 8)
        with db.begin("write") as tx:
            count_sections["n"] = 0
            tx.execute("CREATE (n:T {pk:$p, v:1, s:'x'}) RETURN n.pk", {"p": 100})
            assert count_sections["n"] == 2
            count_sections["n"] = 0
            for offset in range(5):
                tx.execute(
                    "CREATE (n:T {pk:$p, v:1, s:'x'}) RETURN n.pk", {"p": 200 + offset}
                )
            assert count_sections["n"] == 10


def test_relationship_write_statement_opens_two_page_access_sections(
    tmp_path, count_sections
):
    """The Pulse relation shape lands two endpoints and still pays one staging section."""
    with connect(tmp_path / "rel-budget") as db:
        _schema(db)
        _rows(db, 8)
        with db.begin("write") as tx:
            count_sections["n"] = 0
            tx.execute(
                "MATCH (source:T {pk:$a}), (target:T {pk:$b}) "
                "CREATE (source)-[r:R {w:1}]->(target) RETURN source.pk, target.pk",
                {"a": 1, "b": 2},
            )
            assert count_sections["n"] == 2


def test_read_statement_opens_one_page_access_section(tmp_path, count_sections):
    """A read stages nothing, so it never opens a staging or settling section."""
    with connect(tmp_path / "read-budget") as db:
        _schema(db)
        _rows(db, 8)
        with db.begin("read") as tx:
            tx.execute("MATCH (n:T) RETURN n.pk ORDER BY n.pk")
            count_sections["n"] = 0
            tx.execute("MATCH (n:T) RETURN n.pk ORDER BY n.pk")
            assert count_sections["n"] == 1


def test_schema_statement_opens_one_page_access_section(tmp_path, count_sections):
    """DDL carries its own catalog journal and must not take a row-only staging snapshot."""
    with connect(tmp_path / "ddl-budget") as db:
        with db.begin("write") as tx:
            count_sections["n"] = 0
            tx.execute("CREATE NODE TABLE U(pk INT64, PRIMARY KEY(pk))")
            assert count_sections["n"] == 1


@pytest.fixture
def count_free_variables(monkeypatch):
    """Count AST free-name walks at the module the engine reads them from."""
    import okto_grafx.engine.query_engine as engine_module

    tally = {"n": 0}
    original = engine_module.free_variables

    def counted(expression):
        tally["n"] += 1
        return original(expression)

    monkeypatch.setattr(engine_module, "free_variables", counted)
    return tally


def test_order_by_walks_its_expression_once_per_statement(
    tmp_path, count_free_variables
):
    """The names a sort key reads are a plan constant, not a per-row question."""
    with connect(tmp_path / "sort-budget") as db:
        _schema(db)
        _rows(db, 40)
        with db.begin("read") as tx:
            tx.execute("MATCH (n:T) RETURN n.pk, n.v ORDER BY n.v LIMIT 500")
            count_free_variables["n"] = 0
            result = tx.execute("MATCH (n:T) RETURN n.pk, n.v ORDER BY n.v LIMIT 500")
            assert len(result.rows) == 40
            assert count_free_variables["n"] == 1


def test_sort_walk_count_does_not_grow_with_rows(tmp_path, count_free_variables):
    """Twice the rows, the same number of walks: the memo is keyed by the expression."""
    walks = {}
    for size in (20, 80):
        with connect(tmp_path / f"sort-scale-{size}") as db:
            _schema(db)
            _rows(db, size)
            with db.begin("read") as tx:
                tx.execute("MATCH (n:T) RETURN n.pk ORDER BY n.v LIMIT 500")
                count_free_variables["n"] = 0
                result = tx.execute("MATCH (n:T) RETURN n.pk ORDER BY n.v LIMIT 500")
                assert len(result.rows) == size
                walks[size] = count_free_variables["n"]
    assert walks[20] == walks[80] == 1


def test_sort_memo_refuses_an_entry_that_is_not_its_expression():
    """A reused id must not let one expression answer for another."""
    from types import SimpleNamespace

    from okto_grafx.domain.query.ast import Property, Variable
    from okto_grafx.engine.query_engine import _statement_free_variables

    wanted = Property(subject=Variable(name="n"), key="v")
    impostor = Property(subject=Variable(name="other"), key="v")
    context = SimpleNamespace(
        sort_free_variables={id(wanted): (impostor, ("other",))}
    )
    assert _statement_free_variables(wanted, context) == ("n",)
    assert context.sort_free_variables[id(wanted)][0] is wanted


def test_alias_shadowing_still_wins_over_the_bound_variable(tmp_path):
    """The memo answers for an expression, never for a row: shadowing keeps its precedence."""
    with connect(tmp_path / "shadow") as db:
        _schema(db)
        _rows(db, 6)
        with db.begin("read") as tx:
            shadowed = tx.execute(
                "MATCH (n:T) WITH n, n.v AS v RETURN n.pk AS pk, -v AS v ORDER BY v LIMIT 500"
            )
            direct = tx.execute(
                "MATCH (n:T) RETURN n.pk AS pk, -n.v AS v ORDER BY v LIMIT 500"
            )
        assert shadowed.rows == direct.rows
        assert [row[1] for row in shadowed.rows] == sorted(row[1] for row in shadowed.rows)
