"""Durable schema-selected vector names survive spelling collisions and reopen."""

import struct
from dataclasses import replace

import pytest

from okto_grafx import connect
from okto_grafx.domain.model.catalog import VECTOR_OWNER_NAMES_CAPABILITY
from okto_grafx.domain.model import catalog as catalog_module
from okto_grafx.domain.model.catalog import Catalog
from okto_grafx.domain.index.definition import automatic_index_definitions, index_definition_matches_table
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxCorruptionDetected, GrafxSchemaVersionMismatch
from okto_grafx.domain.page import crc32c


def collision_catalog(path):
    with connect(path) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE s {dimension:2,metric:'cosine'}")
            tx.execute("CREATE NODE TABLE R(id INT64,embedding VECTOR(s),PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM R TO R,embedding VECTOR(s))")
        return db._catalog.catalog.copy()


def test_owner_name_capability_roundtrip_and_old_inventory_refusal(tmp_path, monkeypatch):
    catalog = collision_catalog(tmp_path / "db")
    raw = catalog.serialize()
    assert struct.unpack_from("<Q", raw, 28)[0] & (1 << 25)
    restored = Catalog.deserialize(raw)
    assert restored == catalog and restored.copy().serialize() == raw
    assert restored.table("R", kind="node").vector_identity_names is False
    assert restored.table("R", kind="rel").vector_identity_names is True
    monkeypatch.setattr(catalog_module, "_KNOWN_CAPABILITY_BITS", catalog_module._KNOWN_CAPABILITY_BITS & ~(1 << 25))
    with pytest.raises(GrafxSchemaVersionMismatch):
        Catalog.deserialize(raw)


def test_missing_capability_and_unknown_naming_flags_are_not_inferred(tmp_path):
    catalog = collision_catalog(tmp_path / "db")
    raw = catalog.serialize()
    catalog._required_capabilities -= {VECTOR_OWNER_NAMES_CAPABILITY}
    catalog._serialized_memo = None  # Deliberate corrupt in-memory fixture, not a public mutation.
    with pytest.raises(GrafxConfigurationError, match="capability"):
        catalog.serialize()
    # This fixture has no table-body extension before the per-table owner flag.
    _, end = catalog_module._decode_table(raw, 44)
    for flag in (2, 255):
        changed = bytearray(raw[:-4])
        changed[end] = flag
        changed = bytes(changed)
        with pytest.raises(GrafxCorruptionDetected):
            Catalog.deserialize(changed + struct.pack("<I", crc32c(changed)))


def test_old_vector_spelling_cannot_pass_as_generic_custom_provenance(tmp_path):
    catalog = collision_catalog(tmp_path / "db")
    table = catalog.table("R", kind="rel")
    definition = next(index for index in automatic_index_definitions(table) if index.name.startswith("_grafx_vec_"))
    assert index_definition_matches_table(definition, table)
    assert not index_definition_matches_table(replace(definition, name="vector_R_s"), table)


def test_overlong_derived_vector_name_uses_bounded_identity(tmp_path):
    path = tmp_path / "db"
    space = "s" * 120
    with connect(path) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute(f"CREATE VECTOR SPACE {space} {{dimension:2,metric:'cosine'}}")
            tx.execute(f"CREATE NODE TABLE T(id INT64,v VECTOR({space}),PRIMARY KEY(id))")
            tx.execute("CREATE(:T {id:1,v:$v})", {"v":[1.0,0.0]})
        assert db.vectors.index(space).name == "_grafx_vec_t1_p1"
    with connect(path) as db:
        with db.begin("read") as tx:
            assert db.search_vectors(tx, space=space, query=[1.0,0.0], k=1).hits[0].score == 1.0
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("flag", [1, None, "true"])
def test_identity_naming_flag_requires_exact_boolean(tmp_path, flag):
    table = collision_catalog(tmp_path / "db").table("R",kind="rel")
    with pytest.raises(GrafxConfigurationError, match="boolean"):
        replace(table,vector_identity_names=flag)


def test_new_custom_indexes_cannot_claim_reserved_vector_prefix(tmp_path):
    with connect(tmp_path / "db") as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE NODE TABLE R(id INT64,PRIMARY KEY(id))")
        before = db._catalog.catalog.serialize()
        with pytest.raises(GrafxConfigurationError) as caught:
            db.create_index("_grafx_vec_custom", "R", ("id",))
        assert caught.value.details["reason"] == "reserved_index_prefix"
        assert db._catalog.catalog.serialize() == before
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_history_retains_vector_naming_model_and_rejects_lost_metadata(tmp_path, codec):
    from okto_grafx.engine.system_history_store import HistoryChange, _decode_change, validate_history_model

    path = tmp_path / "db"
    collision_catalog(path)
    tables = (("node","R"),("rel","R"))
    with connect(path, codec=codec) as db:
        db.enable_commit_history()
        db.enable_system_history(tables)
        before = db.commit_history().entries[-1].identity
        old = db.system_as_of(before, tables=tables)
        assert {s.kind:s.vector_identity_names for s in old.schemas} == {"node":False,"rel":True}
        table = db._catalog.catalog.table("R", kind="rel")
        change = HistoryChange(table,0,4,())
        assert b"GXHM02\x04\0\0" in change.encode()
        assert _decode_change(change.encode()) == change
        validate_history_model(change, db._catalog.catalog)
        with pytest.raises(GrafxCorruptionDetected):
            validate_history_model(replace(change,table=replace(table,vector_identity_names=False)), db._catalog.catalog)
        with db.begin("write") as tx:
            tx.execute("CREATE(a:R {id:1,embedding:$v})-[:R {embedding:$w}]->(a)",
                       {"v":[1.0,0.0],"w":[0.0,1.0]})
        assert db.verify("all").findings == ()
        db.checkpoint()
    with connect(path,codec=codec) as db:
        assert db.system_as_of(before,tables=tables).schemas == old.schemas
        assert db.verify("all").findings == ()


@pytest.mark.parametrize("operation", ["transfer", "copy"])
@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_logical_transport_rederives_vector_names_for_target_identities(tmp_path, codec, operation):
    from okto_grafx.transfer import export_graph, import_graph
    from okto_grafx.catalog_copy import capture_copy, copy_graph, prepare_copy_target

    def schema(db, reverse):
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE s {dimension:2,metric:'cosine'}")
            tx.execute("CREATE NODE TABLE Anchor(id INT64,PRIMARY KEY(id))")
            node = "CREATE NODE TABLE R(id INT64,embedding VECTOR(s),PRIMARY KEY(id))"
            edge = "CREATE REL TABLE R(FROM Anchor TO Anchor,embedding VECTOR(s))"
            for ddl in ((edge,node) if reverse else (node,edge)):
                tx.execute(ddl)

    source_path, target_path = tmp_path / "source", tmp_path / "target"
    with connect(source_path,codec=codec) as source:
        schema(source,True)
        if operation == "copy":
            source.enable_commit_history()
        with source.begin("write") as tx:
            tx.execute("CREATE(:R {id:11,embedding:$v})", {"v":[1.0,0.0]})
            tx.execute("CREATE(a:Anchor {id:1})-[:R {embedding:$v}]->(a)", {"v":[0.0,1.0]})
        assert source.catalog.catalog.table("R",kind="node").vector_identity_names
        if operation == "transfer":
            export_graph(source,tmp_path / "artifact")
            import_graph(tmp_path / "artifact",target_path)
        else:
            with source.begin("read") as tx:
                package = capture_copy(tx,tables=(("node","Anchor"),("node","R"),("rel","R")))
            with connect(target_path,codec=codec) as target:
                schema(target,False)
                prepare_copy_target(target)
                assert copy_graph(package,target,idempotency_key="owners").rows == 3
    with connect(target_path,codec=codec) as target:
        assert not target.catalog.catalog.table("R",kind="node").vector_identity_names
        assert target.catalog.catalog.table("R",kind="rel").vector_identity_names
        with target.begin("read") as tx:
            for kind, score in (("node",1.0),("rel",0.0)):
                hits = target.search_vectors(tx,table=(kind,"R"),space="s",query=[1.0,0.0],k=1).hits
                assert len(hits) == 1 and hits[0].score == score
        assert target.verify("all").findings == ()


@pytest.mark.parametrize("codec", ["pure", "numpy"])
@pytest.mark.parametrize("relation_first", [False, True])
def test_same_named_vector_tables_keep_distinct_durable_indexes(tmp_path, codec, relation_first):
    path = tmp_path / "db"
    with connect(path, codec=codec, vector_exact_scan_threshold=0) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            tx.execute("CREATE VECTOR SPACE s {dimension:2,metric:'cosine'}")
            tx.execute("CREATE NODE TABLE Anchor(id INT64,PRIMARY KEY(id))")
            node = "CREATE NODE TABLE R(id INT64,embedding VECTOR(s),PRIMARY KEY(id))"
            edge = "CREATE REL TABLE R(FROM Anchor TO Anchor,id INT64,embedding VECTOR(s))"
            for ddl in ((edge,node) if relation_first else (node,edge)):
                tx.execute(ddl)
            tx.execute("CREATE(:R {id:11,embedding:$v})", {"v":[1.0,0.0]})
            tx.execute("CREATE(a:Anchor {id:1})-[:R {id:22,embedding:$v}]->(a)", {"v":[0.0,1.0]})
        names = {index.name for index in db.vectors.indexes()}
        assert "vector_R_s" in names and len(names) == 2
        assert sum(name.startswith("_grafx_vec_") for name in names) == 1
        assert VECTOR_OWNER_NAMES_CAPABILITY in db._catalog.catalog.required_capabilities()
        assert [table.vector_identity_names for table in db.catalog.catalog.tables()] == [False,False,True]
        db.checkpoint()
    for _ in range(2):
        with connect(path, codec=codec, vector_exact_scan_threshold=0) as db:
            assert {index.name for index in db.vectors.indexes()} == names
            with db.begin("read") as tx:
                for kind, score in (("node",1.0), ("rel",0.0)):
                    hits = db.search_vectors(tx, table=(kind,"R"), space="s", query=[1.0,0.0], k=1).hits
                    assert len(hits) == 1 and hits[0].score == score
            assert db.verify("all").findings == ()
            db.checkpoint()


@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_delimiter_collision_activates_identity_naming_without_graph_name_overlap(tmp_path, codec):
    path = tmp_path / "db"
    with connect(path, codec=codec) as db:
        db.ensure_identity_indexes()
        with db.begin("write") as tx:
            for space in ("s", "a_s"):
                tx.execute(f"CREATE VECTOR SPACE {space} {{dimension:2,metric:'cosine'}}")
            tx.execute("CREATE NODE TABLE T_a(id INT64,v VECTOR(s),PRIMARY KEY(id))")
            tx.execute("CREATE NODE TABLE T(id INT64,v VECTOR(a_s),PRIMARY KEY(id))")
            tx.execute("CREATE(:T_a {id:1,v:$v})", {"v":[1.0,0.0]})
            tx.execute("CREATE(:T {id:2,v:$v})", {"v":[0.0,1.0]})
        assert not db._catalog.catalog.requires_capability("graph_namespaces_v1")
        assert db._catalog.catalog.requires_capability(VECTOR_OWNER_NAMES_CAPABILITY)
        assert db.catalog.catalog.table("T").vector_identity_names is True
    with connect(path, codec=codec) as db:
        with db.begin("read") as tx:
            assert db.search_vectors(tx, space="s", query=[1.0,0.0], k=1).hits[0].score == 1.0
            assert db.search_vectors(tx, space="a_s", query=[1.0,0.0], k=1).hits[0].score == 0.0
        assert db.verify("all").findings == ()
