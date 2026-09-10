"""Resume real process crashes without exposing a partially imported destination."""

import os
import subprocess
import sys

import pytest

from okto_grafx import connect
from okto_grafx.errors import GrafxRecoveryRefused
from okto_grafx.transfer import TransferLimits, export_graph, import_graph


def artifact(tmp_path):
    with connect(tmp_path / "source") as db:
        with db.begin() as tx:
            tx.execute("CREATE NODE TABLE P(id INT64, body STRING, PRIMARY KEY(id))")
            tx.execute("CREATE REL TABLE Link(FROM P TO P)")
            for i in range(6):
                tx.execute(
                    "CREATE (:P {id:$id, body:$body})",
                    {"id": i, "body": f"common word{i}"},
                )
            tx.execute("MATCH (a:P {id:0}), (b:P {id:5}) CREATE (a)-[:Link]->(b)")
        db.create_text_index("text", "P", ("body",), bucket_count=16)
        export_graph(db, tmp_path / "artifact")


@pytest.mark.parametrize(
    "cut",
    [
        "after_batch",
        "before_wal_barrier",
        "after_wal_barrier",
        "before_promotion",
        "after_promotion",
    ],
)
def test_process_crash_resume_and_lost_ack(tmp_path, cut):
    artifact(tmp_path)
    code = """
import os, sys
from pathlib import Path
import okto_grafx.transfer as transfer
import okto_grafx.transfer_resume as resume
root = Path(sys.argv[1])
cut = sys.argv[2]
if cut in ('after_batch', 'before_wal_barrier', 'after_wal_barrier'):
    original = transfer._stage_rows
    calls = 0
    def stage(*args, **kwargs):
        global calls
        calls += 1
        if calls == 2 and cut != 'after_batch':
            from okto_grafx.engine.wal_manager import WalManager
            barrier = WalManager.barrier
            def stop(self):
                if cut == 'after_wal_barrier':
                    barrier(self)
                os._exit(73)
            WalManager.barrier = stop
        result = original(*args, **kwargs)
        if calls == 2:
            os._exit(73)
        return result
    transfer._stage_rows = stage
else:
    original = resume._promote
    def promote(*args):
        if cut == 'after_promotion':
            original(*args)
        os._exit(73)
    resume._promote = promote
transfer.import_graph(root/'artifact', root/'target', resume_directory=root/'work', limits=transfer.TransferLimits(batch_rows=2))
"""
    env = {**os.environ, "PYTHONPATH": str(__import__("pathlib").Path("src").resolve())}
    proc = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path), cut],
        env=env,
        capture_output=True,
        timeout=40,
    )
    assert proc.returncode == 73, proc.stderr.decode()
    assert (tmp_path / "target").exists() == (cut == "after_promotion")
    report = import_graph(
        tmp_path / "artifact",
        tmp_path / "target",
        resume_directory=tmp_path / "work",
        limits=TransferLimits(batch_rows=1),
    )
    assert report.rows == 7 and len(report.record_id_mapping) == 7
    with connect(tmp_path / "target") as db:
        assert db.execute("MATCH (p:P) RETURN count(p)").rows == ((6,),)
        assert db.execute("MATCH (a:P)-[:Link]->(b:P) RETURN a.id,b.id").rows == (
            (0, 5),
        )
        assert len(db.search_text(index="text", query="common").hits) == 6
        assert not db.verify("all").findings
    assert (
        import_graph(
            tmp_path / "artifact",
            tmp_path / "target",
            resume_directory=tmp_path / "work",
        )
        == report
    )


def test_resume_refuses_destination_identity_and_tampered_prefix(tmp_path, monkeypatch):
    import okto_grafx.transfer_resume as resume

    artifact(tmp_path)
    with monkeypatch.context() as scoped:
        scoped.setattr(
            resume, "_promote", lambda *args: (_ for _ in ()).throw(OSError("cut"))
        )
        with pytest.raises(OSError, match="cut"):
            import_graph(
                tmp_path / "artifact",
                tmp_path / "target",
                resume_directory=tmp_path / "work",
            )
    with pytest.raises(GrafxRecoveryRefused):
        import_graph(
            tmp_path / "artifact",
            tmp_path / "other",
            resume_directory=tmp_path / "work",
        )
    with connect(tmp_path / "work" / "database") as db:
        with db.begin() as tx:
            tx.execute("MATCH (p:P {id:0}) SET p.body='changed'")
    with pytest.raises(GrafxRecoveryRefused):
        import_graph(
            tmp_path / "artifact",
            tmp_path / "target",
            resume_directory=tmp_path / "work",
        )


def test_workspace_ownership_duplicate_metadata_and_same_worker_lock(
    tmp_path, monkeypatch
):
    import okto_grafx.transfer_resume as resume
    from okto_grafx.adapters.storage_local import LocalStorageDevice
    from okto_grafx.adapters.coordination_local import LocalProcessCoordinator
    from okto_grafx.adapters.clock_system import SystemClock
    from okto_grafx.errors import GrafxError

    artifact(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    (work / "keep").write_bytes(b"owned by user")
    with pytest.raises(GrafxRecoveryRefused):
        import_graph(tmp_path / "artifact", tmp_path / "target", resume_directory=work)
    assert (work / "keep").read_bytes() == b"owned by user"
    scratch = tmp_path / "unowned-scratch"
    scratch.mkdir()
    (scratch / "resume.next").write_bytes(b"unknown incomplete marker")
    with pytest.raises(GrafxRecoveryRefused):
        import_graph(
            tmp_path / "artifact", tmp_path / "target", resume_directory=scratch
        )
    assert (scratch / "resume.next").read_bytes() == b"unknown incomplete marker"
    work = tmp_path / "owned"
    with monkeypatch.context() as scoped:
        scoped.setattr(
            resume, "_promote", lambda *args: (_ for _ in ()).throw(OSError("cut"))
        )
        with pytest.raises(OSError):
            import_graph(
                tmp_path / "artifact", tmp_path / "target", resume_directory=work
            )
    with LocalStorageDevice(work, create_root=False) as storage:
        lock = LocalProcessCoordinator(
            storage,
            SystemClock(),
            lock_directory=str(work / "control"),
            namespace=str(work),
        )
        with lock.exclusive("logical_import", timeout=0):
            with pytest.raises(GrafxError):
                import_graph(
                    tmp_path / "artifact", tmp_path / "target", resume_directory=work
                )
    marker = work / "resume.json"
    marker.write_text('{"phase":"loading",' + marker.read_text()[1:])
    with pytest.raises(GrafxRecoveryRefused):
        import_graph(tmp_path / "artifact", tmp_path / "target", resume_directory=work)
    assert not (tmp_path / "target").exists()


def test_resume_refuses_unexpected_vector_retirement(tmp_path, monkeypatch):
    import okto_grafx.transfer_resume as resume

    with connect(tmp_path / "source") as db:
        with db.begin() as tx:
            tx.execute("CREATE VECTOR SPACE Vec {dimension:2,metric:'cosine'}")
            tx.execute("CREATE NODE TABLE N(v VECTOR(Vec))")
            tx.execute("CREATE (:N {v:[1.0,0.0]})")
        export_graph(db, tmp_path / "artifact")
    with monkeypatch.context() as scoped:

        def interrupt(*args):
            raise OSError("before publication")

        scoped.setattr(resume, "_promote", interrupt)
        with pytest.raises(OSError):
            import_graph(
                tmp_path / "artifact",
                tmp_path / "target",
                resume_directory=tmp_path / "work",
            )
    with connect(tmp_path / "work" / "database") as db, db.begin() as tx:
        catalog = db._catalog.catalog.copy()
        catalog.retire_space("Vec")
        for page, image in db._catalog.stage(catalog):
            db._transactions._stage_page_image(
                tx._context, db._catalog.file, page, image
            )
    with pytest.raises(GrafxRecoveryRefused):
        import_graph(
            tmp_path / "artifact",
            tmp_path / "target",
            resume_directory=tmp_path / "work",
        )
    assert not (tmp_path / "target").exists()
