"""Expression-local graph reads participate in logical-view schema authority."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.model.schema import ColumnDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.errors import GrafxLedgerError, GrafxUnsupportedOperation


@pytest.fixture
def db(tmp_path):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE R(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM R TO R)")
            tx.execute("CREATE(a:R {id:1})-[:R]->(a)")
        db.views.prepare()
        yield db


def test_exists_expression_retains_physical_schema_dependencies(db):
    view = db.views.create("inside", query="RETURN EXISTS { MATCH(n:R)-[:R]->(m:R) RETURN n } AS present")
    assert len(view.dependencies) == 2
    assert db.views.execute("inside").rows == ((True,),)
    db.add_nullable_column(("rel", "R"), ColumnDef("extra", ValueType.STRING))
    with pytest.raises(GrafxLedgerError) as failure:
        db.views.execute("inside")
    assert failure.value.details["reason"] == "stale_dependency"


@pytest.mark.parametrize("query", [
    "RETURN EXISTS { MATCH(n) RETURN n } AS present",
    "RETURN EXISTS { MATCH(n:R)-[e]->(m:R) RETURN e } AS present",
    "RETURN EXISTS { MATCH(n:_grafx_views_v1) RETURN n } AS present",
])
def test_expression_cannot_hide_unbounded_or_reserved_schema_dependency(db, query):
    before = db.transactions.published_state().last_committed_lsn
    with pytest.raises(GrafxUnsupportedOperation):
        db.views.create("unsafe", query=query)
    assert db.views.get("unsafe") is None
    assert db.transactions.published_state().last_committed_lsn == before


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("query,expected", [
    ("RETURN EXISTS { MATCH(n:R)-[:R]->(m:R) } AS present", ((True,),)),
    ("MATCH(n:R) WHERE (n:R)-[:R]->(:R) RETURN n.id", ((1,),)),
    ("RETURN [(n:R)-[:R]->(m:R) | n.id] AS ids", (((1,),),)),
    ("CALL { MATCH(n:R)-[:R]->(m:R) RETURN n.id AS id } RETURN id", ((1,),)),
    ("RETURN EXISTS { MATCH(n:R) WHERE EXISTS { MATCH(m:R)-[:R]->(o:R) WHERE m.id=n.id } RETURN n AS value UNION ALL MATCH(p:R) RETURN p AS value } AS present", ((True,),)),
])
def test_expression_forms_execute_reopen_and_refuse_changes_to_each_owner(tmp_path, codec, query, expected):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE R(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM R TO R)")
            tx.execute("CREATE(a:R {id:1})-[:R]->(a)")
        db.views.prepare()
        definition = db.views.create("expression", query=query)
        assert len(definition.dependencies) == 2
        assert db.views.execute("expression").rows == expected
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE Unrelated(id INT64,PRIMARY KEY(id))")
        assert db.views.execute("expression").rows == expected
        for kind in ("node", "rel"):
            db.add_nullable_column((kind, "R"), ColumnDef("extra", ValueType.STRING))
            with pytest.raises(GrafxLedgerError) as failure:
                db.views.execute("expression")
            assert failure.value.details["reason"] == "stale_dependency"
            definition = db.views.create("expression", query=query, replace=True)
            assert db.views.execute("expression").rows == expected
        db.checkpoint()
    with connect(path, codec=codec, read_only=True) as db:
        assert db.views.get("expression") == definition
        assert db.views.execute("expression").rows == expected
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("query", [
    "RETURN EXISTS { MATCH(n:Missing) RETURN n } AS present",
    "RETURN EXISTS { MATCH(n:R)-[:Missing]->(m:R) RETURN n } AS present",
])
def test_later_admission_of_missing_explicit_dependency_requires_replacement(db, query):
    db.views.create("missing", query=query)
    assert db.views.execute("missing").rows == ((False,),)
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE Missing(id INT64,PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE Missing(FROM R TO R)")
    with pytest.raises(GrafxLedgerError) as failure:
        db.views.execute("missing")
    assert failure.value.details["reason"] == "stale_dependency"
    db.views.create("missing", query=query, replace=True)
    assert db.views.execute("missing").rows == ((False,),)


def test_expression_registry_read_hidden_below_nested_subquery_is_rejected(db):
    query = "RETURN EXISTS { RETURN EXISTS { MATCH(n:_grafx_views_v1) RETURN n } AS nested } AS present"
    with pytest.raises(GrafxUnsupportedOperation):
        db.views.create("unsafe", query=query)
    assert db.views.get("unsafe") is None


def test_literal_and_parameter_text_is_not_treated_as_query_structure(db):
    from okto_grafx.views import ViewParameter

    text = "EXISTS { MATCH(n:_grafx_views_v1) RETURN n }"
    definition = db.views.create("text", query="RETURN $text AS value",
                                 parameters_schema=(ViewParameter("text", ValueType.STRING),))
    assert definition.dependencies == ()
    assert db.views.execute("text", {"text": text}).rows == ((text,),)


def test_logical_relationship_group_growth_invalidates_expression_view(tmp_path):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE(a:N {id:1})-[:R]->(a)")
        db.views.prepare()
        query = "RETURN EXISTS { MATCH(n:N)-[:R]->(m:N) RETURN n } AS present"
        definition = db.views.create("grouped", query=query)
        assert len(definition.dependencies) == 2
        with db.begin("write") as tx:
            tx.execute("MATCH(n:N) CREATE(:M {id:2})-[:R]->(n)")
        with pytest.raises(GrafxLedgerError) as failure:
            db.views.execute("grouped")
        assert failure.value.details["reason"] == "stale_dependency"
        updated = db.views.create("grouped", query=query, replace=True)
        assert len(updated.dependencies) == 3
        assert db.views.execute("grouped").rows == ((True,),)
