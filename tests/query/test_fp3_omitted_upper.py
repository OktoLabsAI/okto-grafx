"""Omission means complete enumeration or a resource error, never hidden truncation."""

from dataclasses import replace

import pytest

import okto_grafx
from okto_grafx.domain.errors import GrafxPlanError, GrafxQueryBudgetExceeded
from okto_grafx.domain.query.analysis import analyze
from okto_grafx.domain.query.parser import parse


@pytest.fixture
def chain(tmp_path):
    with okto_grafx.connect(tmp_path / "chain") as db:
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE P(id INT64, mark INT64, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE E(FROM P TO P)")
            for identity in range(32):
                tx.execute("CREATE (:P {id:$id})", {"id": identity})
            for identity in range(31):
                tx.execute("MATCH (a:P {id:$a}), (b:P {id:$b}) CREATE (a)-[:E]->(b)",
                           {"a": identity, "b": identity + 1})
        yield db


@pytest.mark.parametrize("bounds", ["*", "*..", "*0..", "*25..", "*30.."])
def test_complete_enumeration_refuses_a_valid_thirty_first_edge(chain, bounds):
    with pytest.raises(GrafxQueryBudgetExceeded) as raised:
        chain.execute(f"MATCH p=(a:P {{id:0}})-[:E{bounds}]->(b:P) RETURN p")
    assert raised.value.details["field"] == "max_traversal_hops"
    assert raised.value.details["limit"] == 30
    assert raised.value.details["observed"] == 31
    assert chain.transactions.open_transactions == 0


def test_explicit_thirty_is_not_the_same_query_and_remains_truncated_by_request(chain):
    rows = chain.execute("MATCH p=(a:P {id:0})-[:E*1..30]->(b:P) RETURN length(p), b.id").rows
    assert sorted(rows) == tuple_rows(range(1, 31))
    rows = chain.execute("MATCH p=(a:P {id:1})-[:E*]->(b:P) RETURN length(p), b.id").rows
    assert sorted(rows) == [(length, length + 1) for length in range(1, 31)]


@pytest.mark.parametrize("pattern", [
    "(a:P {id:31})<-[:E*]-(b:P)",
    "(a:P {id:0})-[:E*]-(b:P)",
])
def test_incoming_and_undirected_omission_cannot_truncate(chain, pattern):
    with pytest.raises(GrafxQueryBudgetExceeded) as raised:
        chain.execute(f"MATCH p={pattern} RETURN length(p)")
    assert raised.value.details["field"] == "max_traversal_hops"


def test_extra_probe_uses_existing_path_and_expansion_quotas(chain, tmp_path):
    query = "MATCH p=(a:P {id:0})-[:E*]->(b:P) RETURN length(p)"
    with okto_grafx.connect(tmp_path / "chain", max_traversal_paths=30) as reader:
        with pytest.raises(GrafxQueryBudgetExceeded) as raised:
            reader.execute(query)
        assert raised.value.details["field"] == "max_traversal_paths"
        assert reader.transactions.open_transactions == 0
    with okto_grafx.connect(tmp_path / "chain", max_traversal_expansions=30) as reader:
        with pytest.raises(GrafxQueryBudgetExceeded) as raised:
            reader.execute(query)
        assert raised.value.details["field"] == "max_traversal_expansions"
        assert reader.transactions.open_transactions == 0


def tuple_rows(values):
    return [(value, value) for value in values]


def test_lower_twenty_five_and_limit_thirty_do_not_inherit_old_twenty_policy(chain):
    rows = chain.execute("MATCH p=(a:P {id:0})-[:E*25..]->(b:P) RETURN length(p) LIMIT 6").rows
    assert sorted(rows) == [(value,) for value in range(25, 31)]
    rows = chain.execute("MATCH p=(a:P {id:0})-[:E*]->(b:P) RETURN length(p) LIMIT 30").rows
    assert sorted(rows) == [(value,) for value in range(1, 31)]


@pytest.mark.parametrize("tail", ["WHERE b.id=31", "WHERE b.id=-1", ""])
def test_filter_cannot_hide_incomplete_enumeration(chain, tail):
    with pytest.raises(GrafxQueryBudgetExceeded):
        chain.execute(f"MATCH p=(a:P {{id:0}})-[:E*]->(b:P) {tail} RETURN b.id")


def test_used_reverse_edge_at_ceiling_is_not_a_valid_extension(chain):
    # From 1 there are exactly thirty forward edges. An incoming reverse of
    # an already-used edge at the terminal node does not extend the trail.
    rows = chain.execute("MATCH p=(a:P {id:1})-[:E*]-(b:P) RETURN length(p), b.id").rows
    assert sorted(rows) == sorted([(1, 0), *[(i, i + 1) for i in range(1, 31)]])


def test_cursor_close_at_ceiling_does_not_probe_but_next_row_does(chain):
    query = "MATCH p=(a:P {id:0})-[:E*]->(b:P) RETURN length(p)"
    with chain.query(query).cursor(batch_size=1) as cursor:
        assert [cursor.fetchone() for _ in range(30)] == [(i,) for i in range(1, 31)]
    assert chain.transactions.open_transactions == 0
    with chain.query(query).cursor(batch_size=1) as cursor:
        assert [cursor.fetchone() for _ in range(30)] == [(i,) for i in range(1, 31)]
        with pytest.raises(GrafxQueryBudgetExceeded):
            cursor.fetchone()
    assert chain.transactions.open_transactions == 0


@pytest.mark.parametrize("consumed", [1, 30])
def test_cancellation_closes_all_open_omitted_traversal_readers(chain, consumed):
    from okto_grafx import CancellationToken
    from okto_grafx.errors import GrafxQueryCancelled

    token = CancellationToken()
    query = "MATCH p=(a:P {id:0})-[:E*]->(b:P) RETURN length(p)"
    with chain.query(query).cursor(batch_size=1, cancellation=token) as cursor:
        for _ in range(consumed):
            assert cursor.fetchone() is not None
        token.cancel()
        with pytest.raises(GrafxQueryCancelled):
            cursor.fetchone()
        assert cursor.closed
    assert chain.transactions.open_transactions == 0


def test_budget_failure_closes_active_stack_and_extra_probe(chain, monkeypatch):
    import okto_grafx.engine.query_engine as engine

    original = engine._edge_steps
    started, closed = [], []

    def instrumented(*args, **kwargs):
        access = original(*args, **kwargs)

        def steps(identity):
            started.append(identity)
            try:
                yield from access(identity)
            finally:
                closed.append(identity)
        return steps

    monkeypatch.setattr(engine, "_edge_steps", instrumented)
    with pytest.raises(GrafxQueryBudgetExceeded):
        chain.execute("MATCH p=(a:P {id:0})-[:E*]->(b:P) RETURN p")
    assert len(started) == len(closed) == 31
    assert set(started) == set(closed)
    assert chain.transactions.open_transactions == 0


def test_blocking_ordering_does_not_turn_limit_into_permission_to_truncate(chain):
    with pytest.raises(GrafxQueryBudgetExceeded):
        chain.execute("MATCH p=(a:P {id:0})-[:E*]->(b:P) RETURN length(p) AS n ORDER BY n DESC LIMIT 1")


def test_snapshot_and_owner_deletion_determine_whether_extension_exists(chain):
    query = "MATCH p=(a:P {id:0})-[:E*]->(b:P) RETURN length(p)"
    with chain.query(query).cursor(batch_size=1) as cursor:
        assert cursor.fetchone() == (1,)
        with chain.begin("write") as tx:
            tx.execute("MATCH (a:P {id:30})-[r:E]->(b:P {id:31}) DELETE r")
            assert len(tx.execute(query).rows) == 30
        with pytest.raises(GrafxQueryBudgetExceeded):
            list(cursor)
    assert len(chain.execute(query).rows) == 30
    assert chain.transactions.open_transactions == 0


def test_budget_failure_after_writes_rolls_back_statement_not_previous_statement(chain, monkeypatch):
    import okto_grafx.engine.query_engine as engine

    original = engine._write_assignments
    applied = []

    def observed(*args, **kwargs):
        result = original(*args, **kwargs)
        applied.append(True)
        return result

    monkeypatch.setattr(engine, "_write_assignments", observed)
    with chain.begin("write") as tx:
        tx.execute("CREATE (:P {id:99})")
        with pytest.raises(GrafxQueryBudgetExceeded):
            tx.execute("MATCH p=(a:P {id:0})-[:E*]->(b:P) SET a.mark=length(p) RETURN b.id")
        assert applied
        assert tx.execute("MATCH (a:P {id:0}) RETURN a.mark").rows == ((None,),)
        assert tx.execute("MATCH (a:P {id:99}) RETURN a.id").rows == ((99,),)
    assert chain.execute("MATCH (a:P {id:0}) RETURN a.mark").rows == ((None,),)


@pytest.mark.parametrize("fields", [
    {"upper_bound_omitted": 1},
    {"upper_bound_omitted": True, "max_hops": 20},
    {"upper_bound_omitted": True, "hop_range_written": False},
])
def test_inconsistent_public_omission_flags_are_refused(fields):
    query = parse("MATCH (a:P)-[:E*]->(b:P) RETURN a.id")
    clause = query.match_clauses[0]
    pattern = clause.patterns[0]
    edge = replace(pattern.relationships[0], **fields)
    forged = replace(query, match_clauses=(replace(clause, patterns=(replace(pattern, relationships=(edge,)),)),))
    with pytest.raises(GrafxPlanError):
        analyze(forged)
