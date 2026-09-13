"""Append-only schema compatibility must not guess row widths or lose old writes."""

import pytest

from okto_grafx import connect
from okto_grafx.domain.model.schema import ColumnDef
from okto_grafx.domain.model.value import ValueType
from okto_grafx.errors import GrafxError


@pytest.fixture
def db(tmp_path):
    with connect(tmp_path / "schema") as db:
        db.ensure_identity_indexes()
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, body STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N, label STRING)")
            tx.execute("CREATE (:N {id:1,body:'first'})")
            tx.execute("CREATE (:N {id:2,body:'second'})")
            tx.execute(
                "MATCH (a:N),(b:N) WHERE a.id=1 AND b.id=2 CREATE (a)-[:R {label:'edge'}]->(b)"
            )
        yield db


def test_old_rows_current_writes_and_schema_roundtrip(db):
    updated = db.add_nullable_column("N", ColumnDef("extra", ValueType.STRING))
    assert updated.schema_version == 2 and updated.schema_layouts == ((1, 2),)
    assert db.catalog.catalog.table_definitions[0].schema_layouts == ((1, 2),)
    assert db.execute("MATCH (n:N) RETURN n.id,n.extra ORDER BY n.id").rows == (
        (1, None),
        (2, None),
    )
    with db.begin() as tx:
        tx.execute("MATCH (n:N) WHERE n.id=1 SET n.extra='new'")
        tx.execute("CREATE (:N {id:3,body:'third',extra:'three'})")
    db.add_nullable_column("N", ColumnDef("number", ValueType.INT64))
    assert db.execute(
        "MATCH (n:N) RETURN n.id,n.extra,n.number ORDER BY n.id"
    ).rows == ((1, "new", None), (2, None, None), (3, "three", None))
    assert not db.verify("all").findings
    path = db.path
    db.checkpoint()
    db.close()
    with connect(path, read_only=True) as reopened:
        assert reopened.execute(
            "MATCH (n:N) RETURN n.id,n.extra,n.number ORDER BY n.id"
        ).rows == ((1, "new", None), (2, None, None), (3, "three", None))
        assert not reopened.verify("all").findings


def test_relationship_old_payloads_and_endpoint_indices(db):
    db.add_nullable_column("R", ColumnDef("weight", ValueType.DOUBLE))
    assert db.execute(
        "MATCH (a:N)-[r:R]->(b:N) RETURN a.id,b.id,r.label,r.weight"
    ).rows == ((1, 2, "edge", None),)
    with db.begin() as tx:
        tx.execute("MATCH (a:N)-[r:R]->(b:N) SET r.weight=2.5")
    assert db.execute("MATCH (a:N)-[r:R]->(b:N) RETURN r.weight").rows == ((2.5,),)
    assert not db.verify("all").findings


def test_old_writer_does_not_commit_short_tuple(db):
    writer = db.begin("write")
    writer.execute("CREATE (:N {id:99,body:'old'})")
    with connect(db.path) as other:
        other.add_nullable_column("N", ColumnDef("extra", ValueType.STRING))
    with pytest.raises(GrafxError):
        writer.commit()
    if writer.active:
        writer.rollback()
    assert db.execute("MATCH (n:N) WHERE n.id=99 RETURN n.id").rows == ()
    assert not db.verify("all").findings


def test_refusals_have_no_durable_effect_and_views_stale(db):
    db.views.prepare()
    db.views.create("n", query="MATCH (n:N) RETURN n.id")
    before = db.transactions.published_state().last_committed_lsn
    for table, column in [
        ("N", ColumnDef("id", ValueType.STRING)),
        ("N", ColumnDef("x", ValueType.STRING, nullable=False)),
        ("absent", ColumnDef("x", ValueType.STRING)),
        ("_grafx_views_v1", ColumnDef("x", ValueType.STRING)),
    ]:
        with pytest.raises(GrafxError):
            db.add_nullable_column(table, column)
    assert db.transactions.published_state().last_committed_lsn == before
    db.add_nullable_column("N", ColumnDef("extra", ValueType.STRING))
    with pytest.raises(GrafxError):
        db.views.execute("n")
    db.views.create("n", query="MATCH (n:N) RETURN n.id", replace=True)
    assert len(db.views.execute("n").rows) == 2


@pytest.mark.parametrize(
    "cut,code,version",
    [
        ("before_commit", 71, 1),
        ("after_commit", 72, 2),
        ("before_apply", 73, 2),
        ("after_first_apply", 74, 2),
    ],
)
def test_process_death_schema_and_capability_are_atomic(
    db, cut, code, version, monkeypatch
):
    import os
    from pathlib import Path
    import subprocess
    import sys

    path = db.path
    db.checkpoint()
    db.close()
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2] / "src"))
    run = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("nullable_columns_worker.py")),
            str(path),
            cut,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert run.returncode == code, run.stderr
    if cut == "before_apply":
        import okto_grafx.domain.model.catalog as catalog_module

        catalog_file = Path(path) / "catalog.dat"
        before = catalog_file.read_bytes()
        with monkeypatch.context() as old_reader:
            old_reader.setattr(
                catalog_module,
                "_KNOWN_CAPABILITY_BITS",
                catalog_module._KNOWN_CAPABILITY_BITS & ~(1 << 13),
            )
            with pytest.raises(GrafxError):
                connect(path)
        assert catalog_file.read_bytes() == before
    for _ in range(2):
        with connect(path) as recovered:
            table = recovered._catalog.catalog.table("N")
            assert table.schema_version == version
            assert (
                "nullable_columns_v1"
                in recovered._catalog.catalog.required_capabilities()
            ) is (version == 2)
            query = (
                "MATCH (n:N) RETURN n.id,n.extra ORDER BY n.id"
                if version == 2
                else "MATCH (n:N) RETURN n.id ORDER BY n.id"
            )
            assert recovered.execute(query).rows == (
                ((1, None), (2, None)) if version == 2 else ((1,), (2,))
            )
            assert not recovered.verify("all").findings
            recovered.checkpoint()


def test_existing_and_new_indexes_fts_and_copy_export(db, tmp_path):
    from okto_grafx.transfer import export_graph, import_graph

    db.create_text_index("body_text", "N", ("body",))
    db.add_nullable_column("N", ColumnDef("extra", ValueType.STRING))
    assert len(db.search_text(index="body_text", query="first").hits) == 1
    db.create_index("extra_index", "N", ("extra",))
    with db.begin() as tx:
        tx.execute("MATCH (n:N) WHERE n.id=1 SET n.extra='needle'")
    assert db.execute("MATCH (n:N) WHERE n.extra='needle' RETURN n.id").rows == ((1,),)
    export_graph(db, tmp_path / "export")
    import_graph(tmp_path / "export", tmp_path / "destination")
    with connect(tmp_path / "destination") as restored:
        assert restored.execute(
            "MATCH (n:N) RETURN n.id,n.extra ORDER BY n.id"
        ).rows == ((1, "needle"), (2, None))
        assert not restored.verify("all").findings


def test_physical_backup_retains_layout_and_read_only_refuses(db, tmp_path):
    from okto_grafx.backup import create_backup, restore_backup

    db.add_nullable_column("N", ColumnDef("extra", ValueType.STRING))
    create_backup(db, tmp_path / "backup")
    db.close()
    restore_backup(
        tmp_path / "backup", tmp_path / "restore", confirm_original_offline=True
    )
    with connect(tmp_path / "restore", read_only=True) as restored:
        assert restored._catalog.catalog.table("N").schema_layouts == ((1, 2),)
        assert restored.execute("MATCH (n:N) RETURN n.extra").rows == ((None,), (None,))
        with pytest.raises(GrafxError):
            restored.add_nullable_column("N", ColumnDef("blocked", ValueType.STRING))


def test_legacy_requires_explicit_activation(tmp_path):
    with connect(tmp_path / "legacy") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE N(id INT64, PRIMARY KEY(id))")
        with pytest.raises(GrafxError):
            db.add_nullable_column("N", ColumnDef("extra", ValueType.STRING))
        db.ensure_identity_indexes()
        assert (
            db.add_nullable_column(
                "N", ColumnDef("extra", ValueType.STRING)
            ).schema_version
            == 2
        )


def test_registry_format_old_reader_and_history_bounds(db, monkeypatch):
    from dataclasses import replace
    import okto_grafx.domain.model.catalog as module
    from okto_grafx.domain.model.catalog import Catalog

    db.add_nullable_column("N", ColumnDef("extra", ValueType.STRING))
    catalog = db._catalog.catalog
    image = catalog.serialize()
    assert Catalog.deserialize(image).table("N") == catalog.table("N")
    with monkeypatch.context() as old_reader:
        old_reader.setattr(
            module, "_KNOWN_CAPABILITY_BITS", module._KNOWN_CAPABILITY_BITS & ~(1 << 13)
        )
        with pytest.raises(GrafxError):
            Catalog.deserialize(image)
    for layouts in (((1, 1),), ((2, 2),), ((True, 2),), ((1, 2), (1, 2))):
        with pytest.raises(GrafxError):
            replace(catalog.table("N"), schema_layouts=layouts)
    with pytest.raises(GrafxError):
        replace(
            catalog.table("N"),
            columns=(
                *catalog.table("N").columns[:-1],
                ColumnDef("extra", ValueType.STRING, nullable=False),
            ),
        )


def test_unknown_or_future_heap_schema_still_refused(db):
    from okto_grafx.domain.model.record import RecordHeader

    db.add_nullable_column("N", ColumnDef("extra", ValueType.STRING))
    table = db._catalog.catalog.table("N")
    for version in (3, 65535):
        header = RecordHeader(
            record_id=1,
            xmin=1,
            xmax=0,
            prev_version=0,
            payload_len=0,
            schema_version=version,
        )
        with pytest.raises(GrafxError):
            db._heap._validated_payload(table, header, b"")


def test_failed_staging_rolls_back_and_addition_never_rewrites_heap(db, monkeypatch):
    from okto_grafx.engine.query_engine import QueryEngine
    from okto_grafx.engine.heap_store import HeapStore

    original = QueryEngine.add_nullable_column
    before = db._catalog.catalog.serialize()

    def fail_after_stage(engine, *args):
        original(engine, *args)
        raise RuntimeError("injected after schema stage")

    with monkeypatch.context() as scoped:
        scoped.setattr(QueryEngine, "add_nullable_column", fail_after_stage)
        with pytest.raises(GrafxError):
            db.add_nullable_column("N", ColumnDef("failed", ValueType.STRING))
    assert db._catalog.catalog.serialize() == before

    def forbidden(*args, **kwargs):
        raise AssertionError("nullable append must not rewrite a heap row")

    with monkeypatch.context() as scoped:
        for operation in ("insert", "update", "delete"):
            scoped.setattr(HeapStore, operation, forbidden)
        db.add_nullable_column("N", ColumnDef("extra", ValueType.STRING))
    assert db.execute("MATCH (n:N) RETURN n.extra").rows == ((None,), (None,))
    assert not db.verify("all").findings


def test_concurrent_schema_additions_do_not_overwrite_each_other(db, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from okto_grafx.engine.query_engine import QueryEngine

    original = QueryEngine.add_nullable_column
    barrier = Barrier(2)

    def stage(engine, *args):
        result = original(engine, *args)
        barrier.wait(timeout=15)
        return result

    def append(name):
        with connect(db.path) as participant:
            try:
                return participant.add_nullable_column(
                    "N", ColumnDef(name, ValueType.STRING)
                )
            except GrafxError as error:
                return error

    with monkeypatch.context() as scoped:
        scoped.setattr(QueryEngine, "add_nullable_column", stage)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(append, ("first_extra", "second_extra")))
    winners = [r for r in results if not isinstance(r, Exception)]
    assert len(winners) == 1
    # Observation facades describe the handle's adopted catalog; a native read
    # first adopts the other participant's committed publication.
    assert db.execute("MATCH (n:N) RETURN count(*)").rows == ((2,),)
    assert db.catalog.catalog.table_definitions[0] == winners[0]
    assert not db.verify("all").findings


def test_catalog_copy_uses_current_decoded_values_after_evolution(db, tmp_path):
    from okto_grafx.catalog_copy import capture_copy, copy_graph, prepare_copy_target

    db.enable_commit_history()
    db.add_nullable_column("N", ColumnDef("extra", ValueType.STRING))
    with db.begin("read") as reader:
        package = capture_copy(reader, tables=("N", "R"))
    with connect(tmp_path / "target") as target:
        with target.begin() as tx:
            tx.execute(
                "CREATE NODE TABLE N(id INT64, body STRING, extra STRING, PRIMARY KEY(id))"
            )
            tx.execute("CREATE REL TABLE R(FROM N TO N, label STRING)")
        prepare_copy_target(target)
        receipt = copy_graph(package, target, idempotency_key="evolved")
        assert receipt.rows == 3
        assert target.execute("MATCH (n:N) RETURN n.id,n.extra ORDER BY n.id").rows == (
            (1, None),
            (2, None),
        )
        assert copy_graph(package, target, idempotency_key="evolved").replayed
        assert not target.verify("all").findings


def test_maximum_prior_layouts_are_bounded_and_failure_keeps_catalog(db):
    from okto_grafx.domain.model.catalog import Catalog

    candidate = db._catalog.catalog.copy()
    for number in range(64):
        candidate.add_nullable_column("N", ColumnDef(f"col_{number}", ValueType.INT64))
    image = candidate.serialize()
    assert len(Catalog.deserialize(image).table("N").schema_layouts) == 64
    with pytest.raises(GrafxError):
        candidate.add_nullable_column("N", ColumnDef("overflow", ValueType.INT64))
    assert candidate.serialize() == image
    assert db._catalog.catalog.table("N").schema_version == 1


def test_compound_landing_charge_includes_virtual_null_columns():
    from dataclasses import replace
    from okto_grafx.domain.model.schema import TableDef, encode_tuple
    from okto_grafx.engine.query_engine import (
        _owner_landing_result_bytes, _OWNER_LANDING_RESULT_BASE_BYTES,
        _OWNER_LANDING_PAYLOAD_MULTIPLIER, HeapVersion,
    )

    table = TableDef(1, "Compound", "node", (ColumnDef("id", ValueType.INT64), ColumnDef("props", ValueType.MAP), ColumnDef("extra", ValueType.STRING)), schema_version=2, schema_layouts=((1, 2),))
    prior = replace(table, columns=table.columns[:2], schema_version=1, schema_layouts=())
    values = (1, {}, None)
    version = HeapVersion(record_id=1, xmin=1, xmax=0, values=values, prev=None, schema_version=1, deleted=False, table_id=1)
    object.__setattr__(version, "_stored_payload_bytes", len(encode_tuple(prior, values[:2])))
    found = (None, version)
    assert _owner_landing_result_bytes(table, found) == (
        _OWNER_LANDING_RESULT_BASE_BYTES
        + len(encode_tuple(table, found[1].values)) * _OWNER_LANDING_PAYLOAD_MULTIPLIER
    )
