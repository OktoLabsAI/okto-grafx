"""Physical backup cut, manifest refusal and offline same-UUID restore contracts."""

import json
import threading
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from okto_grafx import CommitMetadata, connect
from okto_grafx.backup import create_backup, restore_backup
from okto_grafx.domain.errors import GrafxConfigurationError, GrafxRecoveryRefused


def put(db, text):
    """One public committed update with provenance."""
    with db.transaction(metadata=CommitMetadata(origin="backup-test")) as tx:
        tx.execute("CREATE (:P {id:$id, value:$v})", {"id": text, "v": text * 1500})
    return tx.report.csn


def seed(root):
    """Return an indexed, journal-enabled source with overflow records."""
    db = connect(root, page_size=512)
    with db.transaction() as tx:
        tx.execute("CREATE NODE TABLE P(id STRING, value STRING, PRIMARY KEY(id))")
    db.ensure_identity_indexes()
    db.enable_commit_history()
    put(db, "a")
    return db


def test_backup_restore_identity_provenance_and_writable_reopen(tmp_path):
    source = tmp_path / "db"
    backup = tmp_path / "backup"
    with seed(source) as db:
        identity = db.identity
        history = db.commit_history()
        report = create_backup(db, backup)
        assert report.database_uuid == identity.database_uuid.hex()
        assert not (
            backup / "grafx.meta"
        ).exists()  # Artifact is not an openable same-UUID fork.
    restored = tmp_path / "restored"
    assert (
        restore_backup(backup, restored, confirm_original_offline=True).checkpoint_lsn
        == report.checkpoint_lsn
    )
    with connect(restored, page_size=512) as db:
        assert db.identity == identity
        assert db.commit_history() == history
        assert db.execute("MATCH (p:P) RETURN p.id").rows == (("a",),)
        put(db, "b")
        assert db.verify("all").findings == ()


def test_backup_cut_with_writer_waiting_and_writer_progress_during_artifact_io(
    tmp_path, monkeypatch
):
    import okto_grafx.backup as module
    from okto_grafx.adapters.storage_local import LocalStorageDevice

    source = tmp_path / "db"
    captured = threading.Event()
    attempted = threading.Event()
    committed = threading.Event()
    original_read = LocalStorageDevice.read_log
    original_put = module._put

    def read(storage, file, offset, length):
        """Give a competing writer the opportunity to publish during the physical cut."""
        if storage.root == str(source) and file == "heap.dat" and not captured.is_set():
            captured.set()
            assert attempted.wait(5)
            assert not committed.is_set()
        return original_read(storage, file, offset, length)

    def output(storage, name, payload):
        """Destination IO must run with no source commit fence retained."""
        if name.startswith("objects/"):
            assert committed.wait(10)
        return original_put(storage, name, payload)

    with seed(source) as db, connect(source, page_size=512) as writer:

        def write():
            assert captured.wait(5)
            attempted.set()
            put(writer, "b")
            committed.set()

        monkeypatch.setattr(LocalStorageDevice, "read_log", read)
        monkeypatch.setattr(module, "_put", output)
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(write)
            create_backup(db, tmp_path / "backup")
            future.result(timeout=10)
        assert db.execute("MATCH (p:P) RETURN count(p)").rows == ((2,),)
    restore_backup(
        tmp_path / "backup", tmp_path / "restored", confirm_original_offline=True
    )
    with connect(tmp_path / "restored", page_size=512) as restored:
        assert restored.execute("MATCH (p:P) RETURN p.id").rows == (("a",),)


@pytest.mark.parametrize(
    "failure",
    ["object", "path", "missing", "uuid", "cut", "format", "bool_size", "duplicate"],
)
def test_restore_refuses_corrupt_or_forged_artifact_before_promotion(tmp_path, failure):
    backup = tmp_path / "backup"
    with seed(tmp_path / "db") as db:
        create_backup(db, backup)
    manifest = json.loads((backup / "manifest.json").read_bytes())
    if failure == "object":
        (backup / manifest["files"][0]["object"]).write_bytes(b"corrupt")
    elif failure == "path":
        manifest["files"][0]["name"] = "../escape"
    elif failure == "missing":
        manifest["files"] = []
    elif failure == "uuid":
        manifest["database_uuid"] = "11" * 16
    elif failure == "cut":
        manifest["checkpoint_lsn"] += 1
    elif failure == "format":
        manifest["format"] = "future"
    elif failure == "bool_size":
        manifest["files"][0]["size"] = True
    raw = json.dumps(manifest)
    if failure == "duplicate":
        raw = raw[:-1] + ', "format": "okto-grafx-physical-1"}'
    (backup / "manifest.json").write_text(raw)
    with pytest.raises(GrafxRecoveryRefused):
        restore_backup(backup, tmp_path / "restored", confirm_original_offline=True)
    assert not (tmp_path / "restored").exists()
    assert not (tmp_path / "escape").exists()


def test_budgets_active_transaction_existing_destination_and_offline_assertion(
    tmp_path,
):
    with seed(tmp_path / "db") as db:
        with pytest.raises(GrafxRecoveryRefused):
            create_backup(db, tmp_path / "backup", max_bytes=1)
        assert not (tmp_path / "backup").exists()
        with db.begin("read"):
            with pytest.raises(GrafxRecoveryRefused):
                create_backup(db, tmp_path / "backup")
        create_backup(db, tmp_path / "backup")
        with pytest.raises(GrafxRecoveryRefused):
            create_backup(db, tmp_path / "backup")
    with pytest.raises(GrafxConfigurationError):
        restore_backup(tmp_path / "backup", tmp_path / "restored")
    with pytest.raises(GrafxRecoveryRefused):
        restore_backup(
            tmp_path / "backup", tmp_path / "db", confirm_original_offline=True
        )


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_interruption_does_not_promote_and_does_not_modify_source(
    tmp_path, monkeypatch, operation
):
    import okto_grafx.backup as module

    backup = tmp_path / "backup"
    with seed(tmp_path / "db") as db:
        create_backup(db, backup)

        def fail(*args, **kwargs):
            raise KeyboardInterrupt("before publication")

        monkeypatch.setattr(module, "_promote", fail)
        with pytest.raises(KeyboardInterrupt):
            if operation == "backup":
                create_backup(db, tmp_path / "next")
            else:
                restore_backup(backup, tmp_path / "next", confirm_original_offline=True)
        assert not (tmp_path / "next").exists()
        assert db.execute("MATCH (p:P) RETURN p.id").rows == (("a",),)


def test_capture_timeout_and_destination_publication_race_fail_closed(
    tmp_path, monkeypatch
):
    import okto_grafx.backup as module

    with seed(tmp_path / "db") as db:
        original_clock = module.monotonic
        ticks = iter((0.0, 100.0))
        monkeypatch.setattr(module, "monotonic", lambda: next(ticks, 100.0))
        with pytest.raises(GrafxRecoveryRefused) as refused:
            create_backup(db, tmp_path / "timed", max_capture_seconds=0.5)
        assert refused.value.details["reason"] == "capture_timeout"
        assert not (tmp_path / "timed").exists()
        monkeypatch.setattr(module, "monotonic", original_clock)
        original_promote = module._promote

        def race(staging, destination):
            destination.mkdir()
            (destination / "keep").write_bytes(b"untouched")
            original_promote(staging, destination)

        monkeypatch.setattr(module, "_promote", race)
        with pytest.raises(GrafxRecoveryRefused):
            create_backup(db, tmp_path / "raced")
        assert (tmp_path / "raced" / "keep").read_bytes() == b"untouched"


def test_backup_refuses_object_write_and_read_back_failures(tmp_path, monkeypatch):
    import okto_grafx.backup as module
    from okto_grafx.adapters.storage_local import LocalStorageDevice

    with seed(tmp_path / "db") as db:
        original = LocalStorageDevice.append_log

        def failed(storage, name, data):
            if name.startswith("objects/"):
                raise OSError("injected disk failure")
            return original(storage, name, data)

        monkeypatch.setattr(LocalStorageDevice, "append_log", failed)
        with pytest.raises(OSError, match="injected disk failure"):
            create_backup(db, tmp_path / "failed")
        assert not (tmp_path / "failed").exists()
        monkeypatch.setattr(LocalStorageDevice, "append_log", original)
        # Corrupt an output payload after capture, retaining its original manifest checksum.
        original_put = module._put

        def corrupt(storage, name, data):
            return original_put(
                storage, name, b"x" * len(data) if name.startswith("objects/") else data
            )

        monkeypatch.setattr(module, "_put", corrupt)
        with pytest.raises(GrafxRecoveryRefused, match="read-back"):
            create_backup(db, tmp_path / "corrupt")
        assert not (tmp_path / "corrupt").exists()


def test_authoritative_source_corruption_is_not_promoted_or_repaired(tmp_path):
    from okto_grafx.domain.errors import GrafxError

    with seed(tmp_path / "db") as db:
        db.checkpoint()
        original = db._storage.read_page("heap.dat", 1)
        damaged = bytearray(original)
        damaged[-1] ^= 1
        db._storage.write_page("heap.dat", 1, bytes(damaged))
        try:
            with pytest.raises(GrafxError):
                create_backup(db, tmp_path / "bad-source")
            assert not (tmp_path / "bad-source").exists()
            assert db._storage.read_page("heap.dat", 1) == bytes(damaged)
        finally:
            db._storage.write_page("heap.dat", 1, original)
            db._storage.durable_barrier("heap.dat")


def test_backup_cut_with_a_foreign_writer_process(tmp_path, monkeypatch):
    import okto_grafx.backup as module
    from okto_grafx.adapters.storage_local import LocalStorageDevice

    source = tmp_path / "db"
    code = """
import sys
from okto_grafx import connect
with connect(sys.argv[1], page_size=512) as db:
    print('ready', flush=True)
    sys.stdin.readline()
    print('attempt', flush=True)
    with db.transaction() as tx:
        tx.execute("CREATE (:P {id:'b',value:'foreign'})")
    print('committed', flush=True)
"""
    with seed(source) as db:
        process = subprocess.Popen(
            [sys.executable, "-c", code, str(source)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert process.stdout.readline().strip() == "ready"
            original_read = LocalStorageDevice.read_log
            original_put = module._put
            signalled = False

            def read(storage, file, offset, length):
                nonlocal signalled
                if storage.root == str(source) and file == "heap.dat" and not signalled:
                    signalled = True
                    process.stdin.write("go\n")
                    process.stdin.flush()
                    assert process.stdout.readline().strip() == "attempt"
                return original_read(storage, file, offset, length)

            def output(storage, name, payload):
                if name.startswith("objects/") and process.poll() is None:
                    stdout, stderr = process.communicate(timeout=10)
                    assert process.returncode == 0, stderr
                    assert "committed" in stdout
                return original_put(storage, name, payload)

            monkeypatch.setattr(LocalStorageDevice, "read_log", read)
            monkeypatch.setattr(module, "_put", output)
            create_backup(db, tmp_path / "backup")
            assert signalled
            assert db.execute("MATCH (p:P) RETURN count(p)").rows == ((2,),)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
    restore_backup(
        tmp_path / "backup", tmp_path / "restored", confirm_original_offline=True
    )
    with connect(tmp_path / "restored", page_size=512) as db:
        assert db.execute("MATCH (p:P) RETURN p.id").rows == (("a",),)


def test_manifest_validation_is_not_disabled_by_python_optimization(tmp_path):
    backup = tmp_path / "backup"
    with seed(tmp_path / "db") as db:
        create_backup(db, backup)
    value = json.loads((backup / "manifest.json").read_bytes())
    value["files"][0]["name"] = "../escape"
    (backup / "manifest.json").write_text(json.dumps(value))
    code = """
import sys
from okto_grafx.backup import restore_backup
from okto_grafx.domain.errors import GrafxRecoveryRefused
try:
    restore_backup(sys.argv[1], sys.argv[2], confirm_original_offline=True)
except GrafxRecoveryRefused:
    print('refused')
else:
    raise RuntimeError('unsafe acceptance')
"""
    result = subprocess.run(
        [sys.executable, "-O", "-c", code, str(backup), str(tmp_path / "restored")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "refused"
    assert not (tmp_path / "restored").exists()


def test_backup_preserves_relationships_vectors_and_compressed_wal_capability(tmp_path):
    from okto_grafx import VectorValue

    with connect(tmp_path / "db", page_size=512) as db:
        with db.transaction() as tx:
            tx.execute("CREATE VECTOR SPACE s {dimension: 4, metric: 'cosine'}")
            tx.execute(
                "CREATE NODE TABLE P(id INT64, embedding VECTOR(s), PRIMARY KEY(id))"
            )
            tx.execute("CREATE REL TABLE R(FROM P TO P, title STRING)")
        db.ensure_identity_indexes()
        db.maintenance.enable_wal_page_compression()
        with db.transaction() as tx:
            for i in (1, 2):
                tx.execute(
                    "CREATE (:P {id:$id,embedding:$v})",
                    {
                        "id": i,
                        "v": VectorValue(
                            values=(1.0, float(i), 0.0, 0.0),
                            space_ref=db.catalog.catalog.space("s").space_id,
                        ),
                    },
                )
            tx.execute(
                "MATCH (a:P {id:1}), (b:P {id:2}) CREATE (a)-[:R {title:$t}]->(b)",
                {"t": "edge" * 500},
            )
        with db.begin("read") as tx:
            expected = tuple(
                hit.record_id
                for hit in db.search_vectors(
                    tx, space="s", query=(1.0, 1.0, 0.0, 0.0), k=2
                ).hits
            )
        create_backup(db, tmp_path / "backup")
    restore_backup(
        tmp_path / "backup", tmp_path / "restored", confirm_original_offline=True
    )
    with connect(tmp_path / "restored", page_size=512) as db:
        assert "wal_record_v2" in db._catalog.catalog.required_capabilities()
        assert db.execute(
            "MATCH (a:P)-[r:R]->(b:P) RETURN a.id, b.id, r.title"
        ).rows == ((1, 2, "edge" * 500),)
        with db.begin("read") as tx:
            actual = tuple(
                hit.record_id
                for hit in db.search_vectors(
                    tx, space="s", query=(1.0, 1.0, 0.0, 0.0), k=2
                ).hits
            )
        assert actual == expected
        assert db.verify("all").findings == ()


def test_process_death_during_artifact_write_leaves_no_promoted_backup(tmp_path):
    with seed(tmp_path / "db"):
        pass
    code = """
import os, sys
from okto_grafx import connect
import okto_grafx.backup as backup
original = backup._put
def cut(storage, name, payload):
    original(storage, name, payload)
    if name.startswith('objects/'):
        os._exit(77)
backup._put = cut
with connect(sys.argv[1], page_size=512) as db:
    backup.create_backup(db, sys.argv[2])
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "db"), str(tmp_path / "backup")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 77, result.stderr
    assert not (tmp_path / "backup").exists()
    assert tuple(tmp_path.glob(".backup.incomplete-*"))
    with connect(tmp_path / "db", page_size=512) as db:
        assert db.execute("MATCH (p:P) RETURN p.id").rows == (("a",),)
        assert db.verify("all").findings == ()
