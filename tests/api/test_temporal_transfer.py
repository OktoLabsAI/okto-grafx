"""Exact temporal values through logical transfer, copy and retained history."""

import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys

import pytest

from okto_grafx import connect, DateValue, LocalTimeValue, TimeValue, LocalDateTimeValue, DateTimeValue, DurationValue
from okto_grafx.transfer import export_graph, import_graph, TransferLimits
from okto_grafx.domain.model.temporal_codec import TEMPORAL_VALUES_CAPABILITY


VALUES = (DateValue(-999999999, 2, 28), LocalTimeValue(86399999999999),
          TimeValue(LocalTimeValue(1), -1234),
          LocalDateTimeValue(DateValue(999999999, 12, 31), LocalTimeValue(999999999)),
          DateTimeValue.from_epoch_parts(-1, 999999999, offset_seconds=1234, zone="Future/Recorded"),
          DurationValue(months=2**63-1, days=-3, seconds=-1, nanoseconds=999999999))
TYPES = ("DATE", "LOCALTIME", "TIME", "LOCALDATETIME", "DATETIME", "DURATION")


def seed_temporals(db, model="typed", *, indexed=False):
    db.ensure_identity_indexes()
    parameters = {f"v{i}": value for i, value in enumerate(VALUES)}
    parameters["bag"] = {"values": list(VALUES), "nested": {"clock": VALUES[4]}}
    with db.begin() as tx:
        if model == "typed":
            columns = ", ".join(f"v{i} {kind}" for i, kind in enumerate(TYPES))
            props = ", ".join(f"v{i}:$v{i}" for i in range(6))
            tx.execute(f"CREATE NODE TABLE N(id INT64, {columns}, PRIMARY KEY(id))")
            tx.execute(f"CREATE REL TABLE R(FROM N TO N, {columns})")
        elif model == "any":
            tx.execute("CREATE NODE TABLE N(id INT64, bag ANY, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE R(FROM N TO N, bag ANY)")
            props = "bag:$bag"
        else:
            props = "bag:$bag"
        label = ":N" if model != "flexible" else ""
        tx.execute(f"CREATE (a{label} {{id:1, {props}}})-[r:R {{{props}}}]->(b{label} {{id:2, {props}}})", parameters)
    if indexed:
        for i in range(6):
            db.create_index(f"temporal_{i}", "N", (f"v{i}",), bucket_count=4)
    return tuple(t.name for t in db.catalog.catalog.tables())


def picture(db):
    return db.execute("MATCH(a)-[r:R]->(b) RETURN labels(a),properties(a),properties(r),labels(b),properties(b)").rows


@pytest.mark.parametrize("model", ["typed", "any", "flexible"])
@pytest.mark.parametrize("codec", ["pure", "numpy"])
def test_logical_transfer_preserves_all_temporals_models_and_recorded_zones(tmp_path, model, codec):
    with connect(tmp_path / "source", codec=codec) as db:
        seed_temporals(db, model, indexed=model == "typed")
        expected = picture(db)
        source_uuid = db.identity.database_uuid
        report = export_graph(db, tmp_path / "artifact", limits=TransferLimits(batch_rows=1))
        assert report.rows == 3
    imported = import_graph(tmp_path / "artifact", tmp_path / "target", limits=TransferLimits(batch_rows=1))
    assert imported.rows == 3 and len(imported.record_id_mapping) == 3
    with connect(tmp_path / "target", codec=codec) as db:
        assert db.identity.database_uuid != source_uuid
        assert db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        assert picture(db) == expected
        if model == "typed":
            for i, value in enumerate(VALUES):
                assert db.execute(f"MATCH(n:N) WHERE n.v{i}=$value RETURN n.id ORDER BY n.id", {"value":value}).rows == ((1,), (2,))
        assert not db.verify("all").findings
        db.checkpoint()
    with connect(tmp_path / "target", read_only=True, codec=codec) as db:
        assert picture(db) == expected


@pytest.mark.parametrize("model", ["typed", "any", "flexible"])
def test_catalog_copy_preserves_temporals_and_idempotent_receipt(tmp_path, model):
    from okto_grafx.catalog_copy import prepare_copy_target, capture_copy, copy_graph
    with connect(tmp_path / "source") as source, connect(tmp_path / "target") as target:
        source.ensure_identity_indexes()
        source.enable_commit_history()
        names = seed_temporals(source, model)
        expected = picture(source)
        seed_temporals(target, model)
        with target.begin() as tx:
            tx.execute("MATCH(n) DETACH DELETE n")
        prepare_copy_target(target)
        with source.begin("read") as tx:
            package = capture_copy(tx, tables=names)
        receipt = copy_graph(package, target, idempotency_key="temporal")
        assert receipt.rows == 3
        assert picture(target) == expected
        assert copy_graph(package, target, idempotency_key="temporal").replayed
        assert target._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        assert not target.verify("all").findings
    with connect(tmp_path / "target") as target:
        assert picture(target) == expected


@pytest.mark.parametrize("model", ["typed", "any", "flexible"])
@pytest.mark.parametrize("indexed", [False, True])
def test_history_backup_and_reopen_preserve_native_temporal_values(tmp_path, model, indexed):
    from okto_grafx import CommitId, TemporalLimits
    from okto_grafx.backup import create_backup, restore_backup
    limits = TemporalLimits(access_path="index" if indexed else "scan")
    with connect(tmp_path / "source") as db:
        names = seed_temporals(db, model)
        db.enable_commit_history()
        db.enable_system_history(names)
        if indexed:
            db.enable_system_history_index()
        baseline = db.commit_history().entries[-1].identity
        original = db.system_as_of(baseline, tables=names, limits=limits)
        expected = picture(db)
        with db.begin() as tx:
            tx.execute("MATCH()-[r:R]->() SET r.v0=date('2024-02-29')" if model == "typed"
                       else "MATCH()-[r:R]->() SET r.bag={updated:duration('P1M')}" )
        changed = CommitId(db.identity.database_uuid, tx.report.csn)
        newer = db.system_as_of(changed, tables=names, limits=limits)
        assert newer.rows != original.rows
        assert db.system_as_of(baseline, tables=names, limits=limits).rows == original.rows
        assert len(db.system_diff(baseline, changed, tables=names, limits=limits).rows) == 1
        create_backup(db, tmp_path / "backup")
        assert not db.verify("all").findings
    restore_backup(tmp_path / "backup", tmp_path / "restored", confirm_original_offline=True)
    for directory in ("source", "restored"):
        with connect(tmp_path / directory) as db:
            assert db.system_as_of(baseline, tables=names, limits=limits).rows == original.rows
            assert db.system_as_of(changed, tables=names, limits=limits).rows == newer.rows
            assert picture(db) != expected
            assert not db.verify("all").findings


@pytest.mark.parametrize("model", ["typed", "any", "flexible"])
@pytest.mark.parametrize("cut", ["after_wal_barrier", "after_promotion"])
def test_temporal_import_resumes_real_crash_without_duplicate_entities(tmp_path, model, cut):
    with connect(tmp_path / "source") as db:
        seed_temporals(db, model)
        expected = picture(db)
        export_graph(db, tmp_path / "artifact")
    code = """
import os, sys
from pathlib import Path
import okto_grafx.transfer as transfer
import okto_grafx.transfer_resume as resume
root, cut = Path(sys.argv[1]), sys.argv[2]
if cut == 'after_wal_barrier':
    original = transfer._stage_rows
    calls = 0
    def stage(*args, **kwargs):
        global calls
        calls += 1
        if calls == 2:
            from okto_grafx.engine.wal_manager import WalManager
            barrier = WalManager.barrier
            def stop(self):
                barrier(self)
                os._exit(73)
            WalManager.barrier = stop
        return original(*args, **kwargs)
    transfer._stage_rows = stage
else:
    original = resume._promote
    def promote(*args):
        original(*args)
        os._exit(73)
    resume._promote = promote
transfer.import_graph(root/'artifact', root/'target', resume_directory=root/'work',
                      limits=transfer.TransferLimits(batch_rows=1))
"""
    proc = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), cut],
        env={**os.environ, "PYTHONPATH": str(Path("src").resolve())},
        capture_output=True, timeout=60,
    )
    assert proc.returncode == 73, proc.stderr.decode()
    assert (tmp_path / "target").exists() == (cut == "after_promotion")
    options = {"resume_directory": tmp_path / "work", "limits": TransferLimits(batch_rows=1)}
    report = import_graph(tmp_path / "artifact", tmp_path / "target", **options)
    assert report.rows == 3 and len(report.record_id_mapping) == 3
    assert import_graph(tmp_path / "artifact", tmp_path / "target", **options) == report
    with connect(tmp_path / "target") as db:
        assert picture(db) == expected
        assert db.execute("MATCH(n) RETURN count(n)").rows == ((2,),)
        assert db._catalog.catalog.requires_capability(TEMPORAL_VALUES_CAPABILITY)
        assert not db.verify("all").findings


@pytest.mark.parametrize("model", ["typed", "any", "flexible"])
@pytest.mark.parametrize("resumable", [False, True])
def test_malformed_temporal_frame_refused_before_destination_or_workspace(tmp_path, model, resumable, monkeypatch):
    from okto_grafx.domain.model.temporal_codec import encode_temporal_value
    from okto_grafx.errors import GrafxCorruptionDetected
    import okto_grafx.transfer as transfer
    import okto_grafx.transfer_resume as resume

    with connect(tmp_path / "source") as db:
        seed_temporals(db, model)
        export_graph(db, tmp_path / "artifact")
    manifest_path = tmp_path / "artifact" / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    item = manifest["objects"][0]
    path = manifest_path.parent / item["file"]
    payload = path.read_bytes()
    frame = encode_temporal_value(VALUES[0])
    assert frame in payload
    # Keep framing/checksum valid; the decoder must reject the impossible date,
    # including when nested in ANY or a flexible property map.
    payload = payload.replace(frame, frame[:1] + struct.pack("<q", 2**63 - 1), 1)
    path.write_bytes(payload)
    item["sha256"] = hashlib.sha256(payload).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    def no_destination(*args, **kwargs):
        pytest.fail("Malformed temporal input must be refused before opening any destination")

    monkeypatch.setattr(transfer, "connect", no_destination)
    monkeypatch.setattr(resume, "connect", no_destination)
    options = {"resume_directory": tmp_path / "work"} if resumable else {}
    with pytest.raises(GrafxCorruptionDetected):
        import_graph(tmp_path / "artifact", tmp_path / "target", **options)
    assert not (tmp_path / "target").exists()
    assert not (tmp_path / "work").exists()
