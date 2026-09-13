"""MERGE matches complete node sets and creates native unlabeled nodes when needed."""

import pytest

import okto_grafx
from okto_grafx.errors import GrafxError


def merge(database, text):
    with database.begin("write") as tx:
        return tx.execute(text)


@pytest.fixture
def db(tmp_path):
    with okto_grafx.connect(tmp_path / "merge") as database:
        database.maintenance.ensure_identity_indexes()
        with database.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64, v STRING, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id INT64, v STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE T1(FROM A TO B)")
            tx.execute("CREATE REL TABLE T2(FROM B TO A)")
            tx.execute("CREATE (:A {id:1,v:'same'}),(:A {id:2,v:'same'}),(:B {id:1,v:'other'})")
        yield database


def test_untyped_merge_returns_every_qualified_node_per_input(db):
    result = merge(db, "MATCH(a) MERGE(b) RETURN a,b")
    assert len(result.rows) == 9
    assert len({row[1] for row in result.rows}) == 3
    assert db.execute("MATCH(n) RETURN count(*)").rows == ((3,),)


@pytest.mark.parametrize("pattern", ["(n:A)", "(n:A {})", "(n:A {v:'same'})"])
def test_typed_merge_matches_all_not_first_and_needs_no_missing_pk(pattern, db):
    assert merge(db, f"MERGE {pattern} RETURN n.id ORDER BY n.id").rows == ((1,), (2,))
    assert db.execute("MATCH(n:A) RETURN count(*)").rows == ((2,),)


def test_untyped_properties_select_across_tables_without_creation(db):
    assert merge(db, "MERGE(n {id:1}) RETURN labels(n) ORDER BY labels(n)").rows == ((("A",),), (("B",),))
    assert merge(db, "MERGE(n {v:'same'}) RETURN count(*)").rows == ((2,),)
    created = merge(db, "MERGE(n {missing:42}) RETURN n").rows[0][0]
    assert created.labels == () and dict(created.properties) == {"missing": 42}
    assert merge(db, "MERGE(n {missing:42}) RETURN count(n)").rows == ((1,),)


def test_empty_typed_branch_creates_once_and_later_input_sees_it(db):
    assert merge(db, "UNWIND [10,10,11] AS i MERGE(n:A {id:i,v:'new'}) RETURN n.id").rows == ((10,), (10,), (11,))
    assert db.execute("MATCH(n:A) RETURN count(*)").rows == ((4,),)
    assert db.verify("all").findings == ()


def test_owner_overlay_and_independent_reader(db):
    with db.begin("read") as reader:
        assert reader.execute("MATCH(n) RETURN count(*)").rows == ((3,),)
        with db.begin("write") as tx:
            tx.execute("MATCH(n:A {id:2}) DELETE n")
            tx.execute("MATCH(n:A {id:1}) SET n.v='changed'")
            tx.execute("CREATE(:A {id:3,v:'changed'})")
            assert tx.execute("MERGE(n {v:'changed'}) RETURN n.id ORDER BY n.id").rows == ((1,), (3,))
            assert tx.execute("MERGE(n) RETURN count(*)").rows == ((3,),)
            assert reader.execute("MATCH(n:A) RETURN n.v ORDER BY n.id").rows == (("same",), ("same",))
        assert reader.execute("MATCH(n:A) RETURN n.id ORDER BY n.id").rows == ((1,), (2,))


def test_late_untyped_nonfinite_write_rolls_back_prior_invocations_and_reopens(tmp_path):
    path = tmp_path / "rollback"
    with okto_grafx.connect(path) as database:
        database.maintenance.ensure_identity_indexes()
        with database.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE(:A {id:99}),(:A {id:2})")
            with pytest.raises(GrafxError, match="nonfinite"):
                tx.execute("UNWIND [1,2] AS i MERGE(a:A {id:i}) MERGE(b {id:i+1}) SET b.id=CASE WHEN i=1 THEN 2 ELSE 0.0/0.0 END RETURN b")
            assert tx.execute("MATCH(n:A) RETURN n.id ORDER BY n.id").rows == ((2,), (99,))
        assert database.verify("all").findings == ()
    with okto_grafx.connect(path) as database:
        assert database.execute("MATCH(n:A) RETURN n.id ORDER BY n.id").rows == ((2,), (99,))
        assert database.verify("all").findings == ()


def test_no_input_means_no_creation_and_empty_store_can_create_unlabeled():
    with okto_grafx.connect(":memory:") as database:
        assert merge(database, "UNWIND [] AS i MERGE(n) RETURN n").rows == ()
        assert database.catalog.catalog.tables() == ()
        assert merge(database, "MERGE(n) RETURN n").rows[0][0].labels == ()


def test_read_only_transaction_and_cursor_never_run_merge(db):
    with db.begin("read") as tx:
        with pytest.raises(GrafxError):
            tx.execute("MERGE(n) RETURN n")
    with pytest.raises(GrafxError):
        with db.query("MERGE(n:A {id:40}) RETURN n").cursor() as cursor:
            tuple(cursor)
    assert db.execute("MATCH(n:A) RETURN count(*)").rows == ((2,),)


def test_original_match_merge_optional_cardinality(db):
    with db.begin("write") as tx:
        tx.execute("MATCH(n:A {id:2}) DELETE n")
        tx.execute("MATCH(a:A),(b:B) CREATE(a)-[:T1]->(b),(b)-[:T2]->(a)")
    assert merge(db, "MATCH(a) MERGE(b) WITH * OPTIONAL MATCH(a)--(b) RETURN count(*)").rows == ((6,),)


def test_typed_merge_followed_by_set_changes_every_match(db):
    assert merge(db, "MERGE(n:A {v:'same'}) SET n.v='changed' RETURN n.id ORDER BY n.id").rows == ((1,), (2,))
    assert db.execute("MATCH(n:A) RETURN n.v").rows == (("changed",), ("changed",))


def test_merge_creation_evaluates_nondeterministic_properties_once(monkeypatch):
    from okto_grafx.api import assembly

    values = iter((0.25, 0.75))
    observed = []

    def draw():
        value = next(values)
        observed.append(value)
        return value

    monkeypatch.setattr(assembly, "_new_query_random_source", lambda: draw)
    with okto_grafx.connect(":memory:") as database:
        with database.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(v DOUBLE)")
            assert tx.execute("UNWIND [1,2] AS i MERGE(n:N {v:rand()}) RETURN n.v").rows == ((0.25,), (0.75,))
        assert observed == [0.25, 0.75]


@pytest.mark.parametrize("query", [
    "UNWIND [] AS i MERGE(n:A {id:i}) RETURN n",
    "UNWIND [] AS i CREATE(n:A {id:i}) RETURN n",
    "MATCH(n:A) WHERE false SET n.v='x' RETURN n",
    "MATCH(n:A) WHERE false DELETE n",
])
def test_read_only_write_admission_precedes_even_empty_pipeline(db, query):
    with pytest.raises(GrafxError, match="read transaction"):
        db.execute(query)


def test_merge_cardinality_budget_rolls_back_creation_before_late_failure(tmp_path):
    with okto_grafx.connect(tmp_path / "budget", max_intermediate_rows=2) as database:
        with database.begin("write") as tx:
            tx.execute("CREATE NODE TABLE A(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE B(id INT64, PRIMARY KEY(id))")
            tx.execute("CREATE(:A {id:1}),(:A {id:2}),(:A {id:3})")
            with pytest.raises(GrafxError) as raised:
                tx.execute("MERGE(b:B {id:1}) MERGE(a:A) RETURN a.id")
            assert raised.value.details["field"] == "max_intermediate_rows"
            assert tx.execute("MATCH(n:B) RETURN count(*)").rows == ((0,),)
        assert database.verify("all").findings == ()
