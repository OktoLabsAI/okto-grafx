"""Known-kind projection and migration consumers cannot confuse homonymous tables."""

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxConfigurationError, GrafxLedgerError, GrafxQueryBudgetExceeded
from okto_grafx.migrations import SchemaMigration, migrate_schema
from okto_grafx.projections import ProjectionLimits, project_graph


def seed_projection(db):
    db.ensure_identity_indexes()
    with db.begin("write") as tx:
        tx.execute("CREATE NODE TABLE R(id INT64,w STRING,PRIMARY KEY(id))")
        tx.execute("CREATE NODE TABLE M(id INT64,PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE R(FROM R TO M,w DOUBLE)")
        tx.execute("CREATE(:R {id:1,w:'not-an-edge-weight'})")
        tx.execute("CREATE(:R {id:2,w:'not-an-edge-weight'})")
        tx.execute("CREATE(:M {id:1})")
        tx.execute("MATCH(a:R {id:1}),(b:M) CREATE(a)-[:R {w:2.0}]->(b)")
        tx.execute("MATCH(a:R {id:1}),(b:M) CREATE(a)-[:R {w:3.0}]->(b)")


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_projection_preserves_kind_weights_paging_and_independent_snapshot(tmp_path, codec):
    path = tmp_path / "db"
    options = dict(node_tables=("R", "M"), relationship_tables=("R",),
                   weight_columns={"R": "w"}, limits=ProjectionLimits(batch_rows=1))
    with connect(path, codec=codec) as db:
        seed_projection(db)
        with db.begin("read") as pinned:
            with connect(path, codec=codec) as other, other.begin("write") as writer:
                writer.execute("MATCH(a:R {id:2}),(b:M) CREATE(a)-[:R {w:1.0}]->(b)")
            graph = project_graph(db, pinned, **options)
            assert pinned.active and graph.snapshot_lsn == pinned.snapshot.read_lsn
            assert len(graph.nodes) == 3 and len(graph.edges) == 2
            assert graph.weights == (2.0, 3.0)
            assert graph.diagnostics.max_batch_rows == 1
            assert all(graph.nodes[e.source].table == "R" and graph.nodes[e.target].table == "M"
                       for e in graph.edges)
            assert sorted(graph.degrees()) == [0, 2, 2]
        fresh = project_graph(db, **options)
        assert len(fresh.edges) == 3 and fresh.weights == (2.0, 3.0, 1.0)
        source, target = fresh.nodes[fresh.edges[0].source], fresh.nodes[fresh.edges[0].target]
        assert fresh.weighted_shortest_path(source, target).distance == 2.0
        assert db.verify("all").findings == ()
        db.checkpoint()
    with connect(path, codec=codec, read_only=True) as db:
        reopened = project_graph(db, **options)
        assert reopened.nodes == fresh.nodes and reopened.edges == fresh.edges
        assert reopened.weights == fresh.weights
        assert len(project_graph(db, node_tables=("R",)).nodes) == 2
    assert graph.weights == (2.0, 3.0)  # Detached old picture survives all handles.


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_projection_validates_actual_edge_endpoints_and_selected_budget(tmp_path, codec):
    with connect(tmp_path / "db", codec=codec) as db:
        seed_projection(db)
        before = db.transactions.published_state().last_committed_lsn
        with pytest.raises(GrafxConfigurationError, match="endpoints"):
            project_graph(db, node_tables=("R",), relationship_tables=("R",))
        with pytest.raises(GrafxQueryBudgetExceeded):
            project_graph(db, node_tables=("R", "M"), relationship_tables=("R",),
                          limits=ProjectionLimits(max_edges=1, batch_rows=1))
        with pytest.raises(GrafxConfigurationError):
            project_graph(db, node_tables=("M",), relationship_tables=("M",))
        assert db.transactions.published_state().last_committed_lsn == before
        assert len(project_graph(db, node_tables=("R", "M"), relationship_tables=("R",)).edges) == 2


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("ledger_first", [False, True])
def test_migration_ledger_is_node_owned_with_homonymous_relationship(tmp_path, codec, ledger_first):
    path = tmp_path / "db"
    migration = (SchemaMigration(1, ("CREATE NODE TABLE Created(id INT64,PRIMARY KEY(id))",)),)
    with connect(path, codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE(:N {id:1})")
        if ledger_first:
            assert migrate_schema(db, migration, namespace="app").applied == (1,)
        with db.begin("write") as tx:
            tx.execute("CREATE REL TABLE _grafx_migrations_app(FROM N TO N,checksum STRING)")
            tx.execute("MATCH(n:N) CREATE(n)-[:_grafx_migrations_app {checksum:'unowned-edge'}]->(n)")
        before = db.transactions.published_state().last_committed_lsn
        preview = migrate_schema(db, migration, namespace="app", dry_run=True)
        assert preview.pending == (() if ledger_first else (1,))
        assert db.transactions.published_state().last_committed_lsn == before
        result = migrate_schema(db, migration, namespace="app")
        assert result.applied == (() if ledger_first else (1,))
        assert migrate_schema(db, migration, namespace="app").previously_applied == (1,)
        assert db.execute("MATCH(:N)-[e:_grafx_migrations_app]->(:N) RETURN e.checksum").rows == (("unowned-edge",),)
        assert db.verify("all").findings == ()
        db.checkpoint()
    with connect(path, codec=codec, read_only=True) as db:
        assert migrate_schema(db, migration, namespace="app", dry_run=True).previously_applied == (1,)


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_homonymous_edge_cannot_validate_a_corrupt_node_migration_ledger(tmp_path, codec):
    with connect(tmp_path / "db", codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE N(id INT64,PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE _grafx_migrations_app(version INT64,checksum STRING,PRIMARY KEY(version))")
            tx.execute("CREATE REL TABLE _grafx_migrations_app(FROM N TO N,checksum STRING)")
        before = db.transactions.published_state().last_committed_lsn
        with pytest.raises(GrafxLedgerError) as failure:
            migrate_schema(db, (), namespace="app")
        assert failure.value.details["reason"] == "missing_or_invalid_ownership"
        assert db.transactions.published_state().last_committed_lsn == before


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.optional_dependency("pyarrow")
@pytest.mark.optional_dependency("networkx")
def test_detached_graph_exports_preserve_same_named_node_and_relationship(tmp_path, codec):
    pytest.importorskip("pyarrow")
    pytest.importorskip("networkx")
    from okto_grafx.graph_interop import projection_arrow_batches, to_networkx

    with connect(tmp_path / "db", codec=codec) as db:
        seed_projection(db)
        graph = project_graph(db, node_tables=("R", "M"), relationship_tables=("R",),
                              weight_columns={"R": "w"}, limits=ProjectionLimits(batch_rows=1))
    exported = to_networkx(graph)
    assert exported.number_of_nodes() == 3 and exported.number_of_edges() == 2
    assert sorted(edge[3]["weight"] for edge in exported.edges(keys=True, data=True)) == [2.0, 3.0]
    assert all(edge[0][1] == "R" and edge[1][1] == "M" and edge[2][0] == "R"
               for edge in exported.edges(keys=True))
    nodes = [row for batch in projection_arrow_batches(graph, kind="nodes", batch_rows=1)
             for row in batch.to_pylist()]
    edges = [row for batch in projection_arrow_batches(graph, kind="edges", batch_rows=1)
             for row in batch.to_pylist()]
    assert len(nodes) == 3 and len(edges) == 2
    assert all(edge["table"] == "R" and edge["source_table"] == "R" and edge["target_table"] == "M"
               for edge in edges)
    assert sorted(edge["weight"] for edge in edges) == [2.0, 3.0]
