"""DECIMAL declarations and values through history, copy and logical transfer."""

from dataclasses import replace
import hashlib
import json

import pytest

from okto_grafx import connect, CommitId, DateValue, DecimalValue, TemporalLimits
from okto_grafx.backup import create_backup, restore_backup
from okto_grafx.catalog_copy import CopyLimits, capture_copy, copy_graph, prepare_copy_target, _digest
from okto_grafx.domain.model.decimal_codec import DECIMAL_VALUES_CAPABILITY, encode_decimal_value
from okto_grafx.domain.model.value import decode_values, encode_values
from okto_grafx.errors import GrafxConfigurationError, GrafxCorruptionDetected, GrafxError
from okto_grafx.transfer import export_graph, import_graph, TransferLimits


VALUES = (DecimalValue(10**38 - 1, 38, 19), DecimalValue(-12300, 12, 4), DecimalValue(0, 38, 38))
MODELS = ("typed", "any", "flexible", "namespace")


def seed_decimals(db, model="typed", *, indexed=False):
    """Two nodes and one relationship, with exact and nested native values."""
    db.ensure_identity_indexes()
    parameters = {f"v{i}": value for i, value in enumerate(VALUES)}
    parameters["bag"] = {"values": list(VALUES), "nested": {"d": VALUES[1], "date": DateValue(2024, 2, 29)}}
    with db.begin() as tx:
        if model in ("typed", "namespace"):
            columns = ", ".join(f"v{i} DECIMAL({v.precision},{v.scale})" for i, v in enumerate(VALUES))
            props = ", ".join(f"v{i}:$v{i}" for i in range(len(VALUES)))
            tx.execute(f"CREATE NODE TABLE N(id INT64, {columns}, PRIMARY KEY(id))")
            tx.execute(f"CREATE REL TABLE {'GROUP N' if model == 'namespace' else 'R'}(FROM N TO N, {columns})")
        elif model == "any":
            tx.execute("CREATE NODE TABLE N(id INT64, bag ANY, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N, bag ANY)")
            props = "bag:$bag"
        else:
            props = "bag:$bag"
        label = ":N" if model != "flexible" else ""
        edge = "N" if model == "namespace" else "R"
        tx.execute(f"CREATE (a{label} {{id:1, {props}}})-[:{edge} {{{props}}}]->(b{label} {{id:2, {props}}})", parameters)
    if indexed:
        db.create_index("decimal_value", "N", ("v0",), bucket_count=4)
    return tuple((t.kind, t.name) for t in db.catalog.catalog.tables())


def picture(db):
    """Read detached values, endpoint properties and logical labels/types."""
    return db.execute("MATCH(a)-[r]->(b) RETURN labels(a),properties(a),type(r),properties(r),labels(b),properties(b)").rows


def decimal_declarations(tables):
    """Project all column type parameters, including relationship properties."""
    return tuple((t.kind, t.name, tuple((c.name, c.type.name, c.decimal_precision, c.decimal_scale)
                                      for c in t.columns)) for t in tables)


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_logical_transfer_exact_values_declarations_and_indexes(tmp_path, model, codec):
    with connect(tmp_path / "source", codec=codec) as db:
        seed_decimals(db, model, indexed=model in ("typed", "namespace"))
        expected = picture(db)
        declarations = decimal_declarations(db.catalog.catalog.tables())
        source_uuid = db.identity.database_uuid
        assert export_graph(db, tmp_path / "artifact").rows == 3
    report = import_graph(tmp_path / "artifact", tmp_path / "target", limits=TransferLimits(batch_rows=1))
    assert report.rows == 3 and len(report.record_id_mapping) == 3
    with connect(tmp_path / "target", codec=codec) as db:
        assert db.identity.database_uuid != source_uuid
        assert db._catalog.catalog.requires_capability(DECIMAL_VALUES_CAPABILITY)
        assert picture(db) == expected
        assert decimal_declarations(db.catalog.catalog.tables()) == declarations
        if model in ("typed", "namespace"):
            assert db.execute("MATCH(n:N) WHERE n.v0=$v RETURN n.id ORDER BY n.id", {"v": VALUES[0]}).rows == ((1,), (2,))
        assert not db.verify("all").findings
        db.checkpoint()
    with connect(tmp_path / "target", read_only=True, codec=codec) as db:
        assert picture(db) == expected
        assert decimal_declarations(db.catalog.catalog.tables()) == declarations


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_copy_exact_values_snapshot_and_idempotent_receipt(tmp_path, model, codec):
    with connect(tmp_path / "source", codec=codec) as source, connect(tmp_path / "target", codec=codec) as target:
        source.ensure_identity_indexes()
        source.enable_commit_history()
        names = seed_decimals(source, model)
        seed_decimals(target, model)
        with target.begin() as tx:
            tx.execute("MATCH(n) DETACH DELETE n")
        prepare_copy_target(target)
        expected = picture(source)
        with source.begin("read") as tx:
            package = capture_copy(tx, tables=names)
        with target.begin("read") as reader:
            receipt = copy_graph(package, target, idempotency_key="decimal")
            assert receipt.rows == 3
            # Preparation has its own owner/receipt rows; count application IDs.
            assert reader.execute("MATCH(n) WHERE n.id IN [1,2] RETURN count(n)").rows == ((0,),)
            assert target.execute("MATCH(n) WHERE n.id IN [1,2] RETURN count(n)").rows == ((2,),)
        assert picture(target) == expected
        assert copy_graph(package, target, idempotency_key="decimal") == replace(receipt, replayed=True)
        assert not target.verify("all").findings
    with connect(tmp_path / "target", codec=codec) as target:
        assert picture(target) == expected
        assert copy_graph(package, target, idempotency_key="decimal").replayed


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("indexed", (False, True))
@pytest.mark.parametrize("codec", ("pure", "numpy"))
def test_history_versions_delete_recreate_backup_and_reopen(tmp_path, model, indexed, codec):
    limits = TemporalLimits(access_path="index" if indexed else "scan")
    with connect(tmp_path / "source", codec=codec) as db:
        names = seed_decimals(db, model)
        db.enable_commit_history()
        db.enable_system_history(names)
        if indexed:
            db.enable_system_history_index()
        baseline = db.commit_history().entries[-1].identity
        original = db.system_as_of(baseline, tables=names, limits=limits)
        assert decimal_declarations(original.schemas) == decimal_declarations(db.catalog.catalog.tables())
        with db.begin() as tx:
            tx.execute("MATCH()-[r]->() SET r.v1=decimal('2.50',12,4)" if model in ("typed", "namespace")
                       else "MATCH()-[r]->() SET r.bag={value:decimal('2.50',12,4)}")
        updated = CommitId(db.identity.database_uuid, tx.report.csn)
        changed = db.system_as_of(updated, tables=names, limits=limits)
        assert changed.rows != original.rows
        assert len(db.system_diff(baseline, updated, tables=names, limits=limits).rows) == 1
        old_edge = next(row for row in original.rows
                        if row.table_id in {t.table_id for t in original.schemas if t.kind == "rel"})
        edge_schema = next(t for t in original.schemas if t.table_id == old_edge.table_id)
        assert len(db.system_versions(("rel", edge_schema.name), old_edge.record_id, limits=limits).versions) == 2
        with db.begin() as tx:
            tx.execute("MATCH()-[r]->() DELETE r")
        deleted = CommitId(db.identity.database_uuid, tx.report.csn)
        empty_edges = db.system_as_of(deleted, tables=names, limits=limits)
        assert len(empty_edges.rows) == 2
        with db.begin() as tx:
            tx.execute("MATCH(a {id:1}),(b {id:2}) CREATE(a)-[:" + ("N" if model == "namespace" else "R")
                       + (" {v1:$v}" if model in ("typed", "namespace") else " {bag:$v}") + "]->(b)", {"v": VALUES[1]})
        recreated = CommitId(db.identity.database_uuid, tx.report.csn)
        latest = db.system_as_of(recreated, tables=names, limits=limits)
        new_edge = next(row for row in latest.rows if row.table_id == old_edge.table_id)
        assert new_edge.record_id != old_edge.record_id
        assert db.system_as_of(baseline, tables=names, limits=limits).rows == original.rows
        create_backup(db, tmp_path / "backup")
        assert not db.verify("all").findings
    restore_backup(tmp_path / "backup", tmp_path / "restored", confirm_original_offline=True)
    for directory in ("source", "restored"):
        with connect(tmp_path / directory, codec=codec) as db:
            for identity, expected in ((baseline, original), (updated, changed), (deleted, empty_edges), (recreated, latest)):
                actual = db.system_as_of(identity, tables=names, limits=limits)
                assert actual.rows == expected.rows and actual.schemas == expected.schemas
            assert not db.verify("all").findings


@pytest.mark.parametrize("model", ("typed", "any", "flexible"))
@pytest.mark.parametrize("resumable", (False, True))
def test_malformed_decimal_frame_refused_before_destination(tmp_path, model, resumable, monkeypatch):
    import okto_grafx.transfer as transfer
    import okto_grafx.transfer_resume as resume
    with connect(tmp_path / "source") as db:
        seed_decimals(db, model)
        export_graph(db, tmp_path / "artifact")
    manifest_path = tmp_path / "artifact" / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    item = manifest["objects"][0]
    path = manifest_path.parent / item["file"]
    frame = encode_decimal_value(VALUES[0])
    payload = path.read_bytes()
    assert frame in payload
    payload = payload.replace(frame, frame[:1] + b"\x00" + frame[2:], 1)
    path.write_bytes(payload)
    item["sha256"] = hashlib.sha256(payload).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def no_destination(*args, **kwargs):
        pytest.fail("Invalid DECIMAL must refuse before opening destination/workspace")

    monkeypatch.setattr(transfer, "connect", no_destination)
    monkeypatch.setattr(resume, "connect", no_destination)
    with pytest.raises(GrafxCorruptionDetected):
        import_graph(tmp_path / "artifact", tmp_path / "target", **({"resume_directory": tmp_path / "work"} if resumable else {}))
    assert not (tmp_path / "target").exists() and not (tmp_path / "work").exists()


@pytest.mark.parametrize("kind", ("node", "rel"))
@pytest.mark.parametrize("mismatch", ("precision", "scale"))
@pytest.mark.parametrize("resumable", (False, True))
def test_checksums_do_not_authorize_decimal_schema_normalization(tmp_path, kind, mismatch, resumable, monkeypatch):
    import okto_grafx.transfer as transfer
    import okto_grafx.transfer_resume as resume
    with connect(tmp_path / "source") as db:
        seed_decimals(db)
        export_graph(db, tmp_path / "artifact")
    manifest_path = tmp_path / "artifact" / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    table = next(t for t in manifest["schema"]["tables"] if t["kind"] == kind)
    column = next(c for c in table["columns"] if c["name"] == "v1")
    column["decimal_" + mismatch] += 1  # Value still fits exactly; stored declaration no longer matches.
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def no_destination(*args, **kwargs):
        pytest.fail("Declaration mismatch must refuse before opening a destination")

    monkeypatch.setattr(transfer, "connect", no_destination)
    monkeypatch.setattr(resume, "connect", no_destination)
    with pytest.raises(GrafxError) as error:
        import_graph(tmp_path / "artifact", tmp_path / "target", **({"resume_directory": tmp_path / "work"} if resumable else {}))
    assert error.value.details["reason"] == "row_schema_encoding"
    assert not (tmp_path / "target").exists() and not (tmp_path / "work").exists()


@pytest.mark.parametrize("empty", (False, True))
@pytest.mark.parametrize("declaration", ("DECIMAL(13,4)", "DECIMAL(12,5)"))
def test_copy_rejects_different_target_precision_scale_even_when_empty(tmp_path, empty, declaration):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        source.ensure_identity_indexes()
        source.enable_commit_history()
        for db, column in ((source, "DECIMAL(12,4)"), (target, declaration)):
            db.ensure_identity_indexes()
            with db.begin() as tx:
                tx.execute(f"CREATE NODE TABLE N(id INT64,v {column},PRIMARY KEY(id))")
        prepare_copy_target(target)
        if not empty:
            with source.begin() as tx:
                tx.execute("CREATE(:N {id:1,v:$v})", {"v": VALUES[1]})
        with source.begin("read") as tx:
            package = capture_copy(tx, tables=("N",))
        before = target.transactions.published_state().last_committed_lsn
        with pytest.raises(GrafxConfigurationError) as error:
            copy_graph(package, target, idempotency_key="mismatch")
        assert error.value.details["field"] == "target_schema"
        assert target.transactions.published_state().last_committed_lsn == before
        assert target.execute("MATCH(n:N) RETURN count(n)").rows == ((0,),)


def test_copy_digest_binds_parameters_and_rechecksummed_frames_still_refuse(tmp_path):
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        source.ensure_identity_indexes()
        source.enable_commit_history()
        names = seed_decimals(source)
        seed_decimals(target)
        prepare_copy_target(target)
        with source.begin("read") as tx:
            package = capture_copy(tx, tables=names)
        table = package.tables[0]
        columns = tuple(replace(c, decimal_precision=13) if c.name == "v1" else c for c in table.schema.columns)
        changed = (replace(table, schema=replace(table.schema, columns=columns)), *package.tables[1:])
        assert _digest(package.source_commit, changed, package.spaces, CopyLimits()) != package.sha256
        rid, raw = table.rows[0]
        values, _ = decode_values(raw, table.schema.arity)
        values = tuple(DecimalValue(v.coefficient, 13, v.scale) if v == VALUES[1] else v for v in values)
        changed = (replace(table, rows=((rid, encode_values(values)), *table.rows[1:])), *package.tables[1:])
        forged = replace(package, tables=changed, sha256=_digest(package.source_commit, changed, package.spaces, CopyLimits()))
        before = target.transactions.published_state().last_committed_lsn
        expected = picture(target)
        with pytest.raises(GrafxConfigurationError) as error:
            copy_graph(forged, target, idempotency_key="forged")
        assert error.value.details["field"] == "row_schema_encoding"
        assert target.transactions.published_state().last_committed_lsn == before
        assert picture(target) == expected
