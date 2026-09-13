"""Long CREATE pipelines stay flat without losing semantics or transaction bounds."""

from dataclasses import replace

import pytest

from okto_grafx import connect
from okto_grafx.domain.query import CreateSequence
from okto_grafx.domain.query.analysis import analyze
from okto_grafx.domain.query.lexer import tokenize
from okto_grafx.domain.query.parser import parse
from okto_grafx.errors import GrafxError, GrafxParseError, GrafxPlanError, GrafxWriteConflict


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_1024_creates_execute_in_constant_operator_depth_and_reopen(tmp_path, codec):
    query = " ".join(f"CREATE(:N {{v:{index}}})" for index in range(1024))
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        plan = db.explain(query)
        sequence = next(node for node in plan.walk() if type(node) is CreateSequence)
        assert len(sequence.patterns) == 1024
        assert max(depth for _, depth in plan.traverse()) <= 4
        assert db.catalog.catalog.tables() == ()
        with db.begin("write") as tx:
            tx.execute(query)
        assert db.execute("MATCH(n:N) RETURN count(*),sum(n.v)").rows == ((1024, 523776),)
        assert db.verify("all").findings == ()
    with connect(path, codec=codec) as db:
        assert db.execute("MATCH(n:N) RETURN count(*),min(n.v),max(n.v)").rows == ((1024, 0, 1023),)
        assert db.verify("all").findings == ()


def test_independent_syntax_boundaries_and_direct_ast_admission():
    assert len(parse("CREATE() " * 1024).ordered_clauses()) == 1024
    with pytest.raises(GrafxParseError) as failure:
        parse("CREATE() " * 1025)
    assert failure.value.details["field"] == "clauses"
    statement = parse("CREATE()")
    clause = statement.updating_clauses[0]
    malformed = replace(statement, clause_pipeline=(clause,) * 1025, updating_clauses=(clause,) * 1025)
    with pytest.raises(GrafxPlanError) as failure:
        analyze(malformed)
    assert failure.value.details["field"] == "clause"
    assert failure.value.details["value"] == "clauses"
    tokenize("+" * 32768)
    with pytest.raises(GrafxParseError) as failure:
        tokenize("+" * 32769)
    assert failure.value.details["field"] == "tokens"


def test_sequence_keeps_per_input_dependencies_named_paths_and_set_boundary(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            result = tx.execute("UNWIND [1,2] AS i CREATE(a:N {v:i}) CREATE(b:N {v:a.v+1}) "
                                "CREATE p=(a)-[:R]->(b) CREATE(c:N {v:length(p)}) "
                                "SET b.v=b.v+10 CREATE(d:N {v:b.v}) RETURN a.v,b.v,c.v,d.v,p ORDER BY a.v")
        assert tuple(row[:4] for row in result.rows) == ((1, 12, 1, 12), (2, 13, 1, 13))
        assert all(len(row[4].relationships) == 1 for row in result.rows)
        assert db.execute("MATCH(n:N) RETURN count(*)").rows == ((8,),)
        assert db.verify("all").findings == ()


def test_late_failure_rolls_back_all_steps_but_preserves_earlier_statement(tmp_path):
    with connect(tmp_path / "db") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE(:Saved {v:1})")
            query = " ".join(f"CREATE(:N {{v:{index}}})" for index in range(100))
            with pytest.raises(GrafxError):
                tx.execute(query + " CREATE(:Broken {v:0.0/0.0})")
            assert tx.execute("MATCH(n:N) RETURN count(*)").rows == ((0,),)
            assert tx.execute("MATCH(n:Saved) RETURN n.v").rows == ((1,),)
        assert {table.name for table in db.catalog.catalog.tables()} == {"Saved"}
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("budget", [{"max_statement_writes": 5}, {"max_transaction_rows": 5}])
def test_flat_program_cannot_bypass_write_budgets(tmp_path, budget):
    with connect(tmp_path / "db", **budget) as db:
        with db.begin("write") as tx:
            with pytest.raises(GrafxError):
                tx.execute("CREATE(:N) " * 6)
            assert tx.execute("MATCH(n) RETURN count(*)").rows == ((0,),)
            tx.execute("CREATE(:Saved)")
        assert db.execute("MATCH(n) RETURN labels(n)").rows == ((("Saved",),),)
        assert db.verify("all").findings == ()


def test_read_only_and_empty_input_never_install_program_schema(tmp_path):
    with connect(tmp_path / "db") as db:
        query = "CREATE(:A) CREATE(:B)"
        with pytest.raises(GrafxError):
            db.execute(query)
        with db.begin("write") as tx:
            assert tx.execute("UNWIND [] AS i " + query).rows == ()
        assert db.catalog.catalog.tables() == ()


def test_plan_snapshot_owns_its_instruction_program(tmp_path):
    with connect(tmp_path / "db") as db:
        query = "CREATE(:N {v:1}) CREATE(:N {v:2})"
        first, second = db.explain(query), db.explain(query)
        left = next(node for node in first.walk() if type(node) is CreateSequence)
        right = next(node for node in second.walk() if type(node) is CreateSequence)
        assert left.patterns[0] is not right.patterns[0]
        object.__setattr__(left.patterns[0].nodes[0].properties.entries[0].value, "value", 99)
        with db.begin("write") as tx:
            result = tx.execute(query)
            assert any(type(node) is CreateSequence for node in result.plan.walk())
        assert db.execute("MATCH(n:N) RETURN n.v ORDER BY n.v").rows == ((1,), (2,))


def test_reader_snapshot_and_unique_writer_conflict(tmp_path):
    path = tmp_path / "db"
    with connect(path) as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
        with connect(path) as other, db.begin("read") as reader:
            writer = db.begin("write")
            try:
                writer.execute("CREATE(:N {id:1}) CREATE(:N {id:2})")
                with other.begin("write") as winner:
                    winner.execute("CREATE(:N {id:1}) CREATE(:N {id:3})")
                assert reader.execute("MATCH(n:N) RETURN count(*)").rows == ((0,),)
                with pytest.raises(GrafxWriteConflict):
                    writer.commit()
            finally:
                writer.rollback()
        assert db.execute("MATCH(n:N) RETURN n.id ORDER BY n.id").rows == ((1,), (3,))
        assert db.verify("all").findings == ()
