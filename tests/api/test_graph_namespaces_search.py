"""Search keeps node vector/lexical candidates separate from a same-named edge."""

from dataclasses import replace

import pytest

from okto_grafx import HybridSearchOptions, connect


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("edge_first", [False, True])
def test_namespace_vector_hybrid_and_graph_evidence(tmp_path, codec, edge_first):
    path = tmp_path / "db"
    with connect(path, codec=codec, vector_exact_scan_threshold=0) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE semantic {dimension:2,metric:'cosine'}")
            # The reverse-order variant creates the edge against another node
            # before the vector node acquires its overlapping label.
            if edge_first:
                tx.execute("CREATE NODE TABLE Anchor(id INT64,PRIMARY KEY(id))")
                tx.execute("CREATE REL TABLE R(FROM Anchor TO Anchor)")
            tx.execute("CREATE NODE TABLE R(id INT64,body STRING,embedding VECTOR(semantic),PRIMARY KEY(id))")
            if not edge_first:
                tx.execute("CREATE REL TABLE R(FROM R TO R)")
            tx.execute("CREATE(:R {id:1,body:'wal durable',embedding:$v})", {"v":[1.0,0.0]})
            tx.execute("CREATE(:R {id:2,body:'wal',embedding:$v})", {"v":[0.8,0.2]})
            if not edge_first:
                tx.execute("MATCH(a:R {id:1}),(b:R {id:2}) CREATE(a)-[:R]->(b)")
        db.create_text_index("text", "R", ("body",), kind="node")

    for _ in range(2):
        with connect(path, codec=codec, vector_exact_scan_threshold=0) as db:
            with db.begin("read") as reader:
                raw = db.search_vectors(reader, space="semantic", query=[1.0,0.0], k=2)
                ids = {hit.record_id for hit in raw.hits}
                assert len(ids) == 2
                args = dict(table="R", index="text", query="wal", space="semantic",
                            vector=[1.0,0.0], k=2)
                hybrid = db.search_hybrid(reader, **args)
                assert hybrid.regime == "complete"
                assert {hit.record_id for hit in hybrid.hits} == ids
                native = reader.execute("MATCH(n:R) WHERE similarity(n.embedding,$q,space=>'semantic') > -2.0 RETURN n.id ORDER BY n.id",
                                        {"q":[1.0,0.0]})
                assert native.rows == ((1,), (2,))
                if not edge_first:
                    options = HybridSearchOptions(graph_relations=("R",),graph_seeds=(min(ids),),
                                                  graph_weight=1,graph_hops=1,graph_filter=True)
                    indexed = db.search_hybrid(reader, **args, options=options)
                    scanned = db.search_hybrid(reader, **args, options=replace(options,graph_access="scan"))
                    assert indexed.hits == scanned.hits
                    assert {hit.record_id for hit in indexed.hits} == ids
                    assert indexed.graph_regime == "incident_index"
                    assert scanned.graph_regime == "scan"
            assert db.verify("all").findings == ()
            db.checkpoint()
