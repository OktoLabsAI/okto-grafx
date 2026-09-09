"""Native logical transfer round trips and fail-closed publication boundaries."""

import json

import pytest

from okto_grafx import connect
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxRecoveryRefused
from okto_grafx.transfer import TransferLimits, export_graph, import_graph


def seed(path):
    """Small graph with parallel edges, nulls, an empty table and a custom index."""
    db = connect(path, page_size=512)
    with db.begin() as tx:
        tx.execute("CREATE NODE TABLE P(id STRING, text STRING, PRIMARY KEY(id))")
        tx.execute("CREATE NODE TABLE Empty(id INT64, PRIMARY KEY(id))")
        tx.execute("CREATE REL TABLE Link(FROM P TO P, weight INT64)")
        tx.execute("CREATE (:P {id:'a', text:'ação 😀'})")
        tx.execute("CREATE (:P {id:'b', text:null})")
        tx.execute(
            "MATCH (a:P {id:'a'}), (b:P {id:'b'}) CREATE (a)-[:Link {weight:1}]->(b)"
        )
        tx.execute(
            "MATCH (a:P {id:'a'}), (b:P {id:'b'}) CREATE (a)-[:Link {weight:2}]->(b)"
        )
    db.ensure_identity_indexes()
    db.create_index("by_text", "P", ["text"], bucket_count=4)
    return db


def test_roundtrip_fresh_identity_schema_parallel_edges_and_repeat_import(tmp_path):
    with seed(tmp_path / "source") as db:
        expected = db.execute(
            "MATCH (a:P)-[r:Link]->(b:P) RETURN a.id, b.id, r.weight ORDER BY r.weight"
        ).rows
        exported = export_graph(
            db, tmp_path / "artifact", limits=TransferLimits(batch_rows=1)
        )
        assert exported.rows == 4 and exported.tables == 3
        assert exported.target_database_uuid is None
        source_uuid = db.identity.database_uuid.hex()
    for name in ("target", "another"):
        report = import_graph(
            tmp_path / "artifact", tmp_path / name, limits=TransferLimits(batch_rows=1)
        )
        assert report.rows == 4 and report.source_database_uuid == source_uuid
        assert report.target_database_uuid != source_uuid
        with connect(tmp_path / name) as db:
            assert (
                db.execute(
                    "MATCH (a:P)-[r:Link]->(b:P) RETURN a.id, b.id, r.weight ORDER BY r.weight"
                ).rows
                == expected
            )
            assert db.execute("MATCH (p:P) RETURN p.id, p.text ORDER BY p.id").rows == (
                ("a", "ação 😀"),
                ("b", None),
            )
            assert not db.verify("all").findings
            assert "by_text" in [
                i.name for i in db._catalog.catalog.index_definitions()
            ]
            with db.begin() as tx:
                tx.execute("CREATE (:P {id:'after', text:'new writer'})")


@pytest.mark.parametrize(
    "change", ["version", "path", "rows", "digest", "extra", "duplicate"]
)
def test_invalid_manifest_never_promotes(tmp_path, change):
    with seed(tmp_path / "source") as db:
        export_graph(db, tmp_path / "artifact")
    path = tmp_path / "artifact" / "manifest.json"
    data = json.loads(path.read_bytes())
    if change == "version":
        data["format"] = "okto-grafx-logical-99"
    elif change == "path":
        data["objects"][0]["file"] = "../source/heap.dat"
    elif change == "rows":
        data["objects"][0]["rows"] += 1
    elif change == "digest":
        data["objects"][0]["sha256"] = "0" * 64
    elif change == "extra":
        data["authority"] = True
    raw = json.dumps(data)
    if change == "duplicate":
        raw = '{"format":"bad",' + raw[1:]
    path.write_text(raw)
    with pytest.raises(GrafxRecoveryRefused):
        import_graph(path.parent, tmp_path / "target")
    assert not (tmp_path / "target").exists()


def test_existing_destination_preserved_and_export_budget(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep").write_bytes(b"do not replace")
    with seed(tmp_path / "source") as db:
        with pytest.raises(GrafxRecoveryRefused):
            export_graph(db, target)
        with pytest.raises(GrafxRecoveryRefused):
            export_graph(
                db,
                tmp_path / "small",
                limits=TransferLimits(max_bytes=256, max_row_bytes=128),
            )
    assert (target / "keep").read_bytes() == b"do not replace"
    assert not (tmp_path / "small").exists()


@pytest.mark.parametrize(
    "kwargs",
    [{"max_rows": True}, {"batch_rows": 0}, {"max_row_bytes": 2**32}, {"max_rows": 1}],
)
def test_limit_validation(kwargs):
    with pytest.raises(GrafxConfigurationError):
        TransferLimits(**kwargs)


def test_all_value_types_and_retired_space(tmp_path):
    from okto_grafx.domain.model.schema import ColumnDef, EmbeddingSpaceDef, TableDef
    from okto_grafx.domain.model.value import Timestamp, Uuid, ValueType, VectorValue
    from okto_grafx.domain.ports.vectormath import DistanceMetric
    from okto_grafx.transfer import _install_schema, _stage_rows

    spaces = (EmbeddingSpaceDef(7, "Vec", 2, DistanceMetric.COSINE, False),)
    columns = tuple(
        ColumnDef(
            name, kind, nullable=False, vector_space="Vec" if name == "v" else None
        )
        for name, kind in (
            ("i", ValueType.INT64),
            ("b", ValueType.BOOL),
            ("d", ValueType.DOUBLE),
            ("s", ValueType.STRING),
            ("raw", ValueType.BYTES),
            ("xs", ValueType.LIST),
            ("map", ValueType.MAP),
            ("t", ValueType.TIMESTAMP),
            ("u", ValueType.UUID),
            ("v", ValueType.VECTOR_F32),
        )
    )
    tables = (TableDef(9, "Mixed", "node", columns),)
    with connect(tmp_path / "source") as db:
        _install_schema(db, tables, spaces)
        table = db._catalog.catalog.table("Mixed")
        space = db._catalog.catalog.space("Vec")
        values = (
            42,
            True,
            -0.0,
            "héllo",
            b"\0\xff",
            (1, None, "x"),
            {"nested": (False, 3)},
            Timestamp(123),
            Uuid(b"u" * 16),
            VectorValue((1, 2), space.space_id),
        )
        _stage_rows(db, table, [(90, values)])
        with db.begin() as tx:
            cat = db._catalog.catalog.copy()
            cat.retire_space("Vec")
            for page, image in db._catalog.stage(cat):
                db._transactions._stage_page_image(
                    tx._context, db._catalog.file, page, image
                )
        export_graph(db, tmp_path / "artifact")
    import_graph(tmp_path / "artifact", tmp_path / "target")
    with connect(tmp_path / "target") as db, db.begin("read") as tx:
        page = tx.scan_rows_v1("Mixed", limit=10)
        assert len(page.rows) == 1 and page.rows[0].record_id != 90
        assert page.rows[0].values == values
        assert not db._catalog.catalog.space("Vec").is_active
        assert db._catalog.catalog.table("Mixed").columns == columns


def test_unsigned_endpoint_identity_remapping(tmp_path):
    from okto_grafx.transfer import _stage_rows

    with connect(tmp_path / "source") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(value STRING)")
            tx.execute("CREATE REL TABLE R(FROM N TO N, value STRING)")
        _stage_rows(
            db,
            db._catalog.catalog.table("N"),
            [(2**63 + 1, ("unsigned",)), (2**63 - 2, ("node",))],
        )
        # The existing heap endpoint column is signed INT64, unlike node/edge IDs.
        _stage_rows(
            db,
            db._catalog.catalog.table("R"),
            [(2**63 + 2, (2**63 - 2, 2**63 - 2, "loop"))],
        )
        export_graph(db, tmp_path / "artifact")
    report = import_graph(tmp_path / "artifact", tmp_path / "target")
    assert [
        (m.source_record_id, m.target_record_id) for m in report.record_id_mapping
    ] == [(2**63 + 1, 1), (2**63 - 2, 2), (2**63 + 2, 3)]
    with connect(tmp_path / "target") as db:
        assert db.execute(
            "MATCH (a:N)-[r:R]->(b:N) RETURN a.value,r.value,b.value"
        ).rows == (("node", "loop", "node"),)


def test_writer_commits_during_export_and_snapshot_stays_fixed(tmp_path, monkeypatch):
    from okto_grafx import Transaction

    original = Transaction.scan_rows_v1
    committed = []
    with (
        seed(tmp_path / "source") as db,
        connect(tmp_path / "source", page_size=512) as writer,
    ):

        def scan(tx, table, **kwargs):
            page = original(tx, table, **kwargs)
            if not committed:
                with writer.begin() as write:
                    write.execute(
                        "CREATE (:P {id:'concurrent', text:'not in snapshot'})"
                    )
                committed.append(True)
            return page

        monkeypatch.setattr(Transaction, "scan_rows_v1", scan)
        report = export_graph(
            db, tmp_path / "artifact", limits=TransferLimits(batch_rows=1)
        )
        assert committed and report.rows == 4
    import_graph(tmp_path / "artifact", tmp_path / "target")
    with connect(tmp_path / "target") as db:
        assert not db.execute("MATCH (p:P {id:'concurrent'}) RETURN p.id").rows


def test_schema_race_and_failed_promotion_leave_source_intact(tmp_path, monkeypatch):
    import okto_grafx.transfer as transfer
    from okto_grafx import Transaction

    original = Transaction.scan_rows_v1
    with (
        seed(tmp_path / "source") as db,
        connect(tmp_path / "source", page_size=512) as writer,
    ):
        changed = []

        def scan(tx, table, **kwargs):
            page = original(tx, table, **kwargs)
            if not changed:
                with writer.begin() as write:
                    write.execute("CREATE NODE TABLE Added(id INT64)")
                changed.append(True)
            return page

        monkeypatch.setattr(Transaction, "scan_rows_v1", scan)
        with pytest.raises(GrafxRecoveryRefused) as error:
            export_graph(db, tmp_path / "raced")
        assert error.value.details["reason"] == "schema_changed"
        assert not (tmp_path / "raced").exists()
        monkeypatch.setattr(Transaction, "scan_rows_v1", original)
        export_graph(db, tmp_path / "artifact")

    def refuse_promotion(*args):
        raise OSError("injected publication failure")

    monkeypatch.setattr(transfer, "_promote", refuse_promotion)
    with pytest.raises(OSError, match="publication"):
        import_graph(tmp_path / "artifact", tmp_path / "target")
    assert not (tmp_path / "target").exists()
    assert (tmp_path / "source" / "heap.dat").exists()
    assert not list(tmp_path.glob(".target.incomplete-*"))
